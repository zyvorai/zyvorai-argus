# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
from __future__ import annotations

import asyncio

from integrations.mcp import tools as job_tools
from integrations.mcp.client import ZyvorApiError


class _FakeClient:
    """Stands in for ZyvorApiClient — records calls, replays scripted job states."""

    def __init__(self, *, job_states=None, enqueue_error=None):
        self.job_states = list(job_states or [])
        self.enqueue_error = enqueue_error
        self.enqueue_calls = []
        self.get_calls = []
        self.cancel_calls = []

    async def enqueue_job(self, kind, params, *, idempotency_key=None):
        self.enqueue_calls.append((kind, params, idempotency_key))
        if self.enqueue_error:
            raise self.enqueue_error
        return self.job_states[0]

    async def get_job(self, job_id):
        self.get_calls.append(job_id)
        # First call after enqueue already consumed job_states[0]; subsequent
        # get_job calls advance through the rest of the scripted states.
        index = min(len(self.get_calls), len(self.job_states) - 1)
        return self.job_states[index]

    async def cancel_job(self, job_id):
        self.cancel_calls.append(job_id)
        return {"cancel_requested": True, "job_id": job_id}


def test_run_job_returns_terminal_result_immediately():
    client = _FakeClient(job_states=[{"id": "job-1", "kind": "ping", "status": "succeeded", "result": {}}])

    result = asyncio.run(
        job_tools.run_job(client, "ping", {"urls": ["https://example.com"]}, wait_s=5, poll_interval_s=0.01)
    )

    assert result["status"] == "succeeded"
    assert client.enqueue_calls == [("ping", {"urls": ["https://example.com"]}, None)]


def test_run_job_polls_until_terminal():
    client = _FakeClient(
        job_states=[
            {"id": "job-1", "kind": "audit", "status": "queued"},
            {"id": "job-1", "kind": "audit", "status": "running"},
            {"id": "job-1", "kind": "audit", "status": "succeeded", "result": {"grade": "A"}},
        ]
    )

    result = asyncio.run(
        job_tools.run_job(client, "audit", {"url": "https://zyvor.dev"}, wait_s=5, poll_interval_s=0.01)
    )

    assert result["status"] == "succeeded"
    assert result["result"] == {"grade": "A"}
    assert len(client.get_calls) == 2


def test_run_job_returns_running_when_wait_budget_expires():
    client = _FakeClient(job_states=[{"id": "job-1", "kind": "audit", "status": "running"}])

    result = asyncio.run(
        job_tools.run_job(client, "audit", {"url": "https://zyvor.dev"}, wait_s=0.05, poll_interval_s=0.02)
    )

    assert result["status"] == "running"
    assert result["job_id"] == "job-1"
    assert "get_job_status" in result["note"]


def test_run_job_rejects_kind_outside_allowlist():
    client = _FakeClient()

    result = asyncio.run(
        job_tools.run_job(client, "loadtest", {"url": "https://zyvor.dev"}, wait_s=5, poll_interval_s=0.01)
    )

    assert "error" in result
    assert "not allowed" in result["error"]
    assert client.enqueue_calls == []


def test_run_job_surfaces_api_error_without_raising():
    client = _FakeClient(enqueue_error=ZyvorApiError(400, "url not in target allowlist"))

    result = asyncio.run(
        job_tools.run_job(client, "audit", {"url": "https://evil.example"}, wait_s=5, poll_interval_s=0.01)
    )

    assert result == {"error": "url not in target allowlist", "status_code": 400}


def test_run_smoke_test_sends_no_url_param():
    client = _FakeClient(job_states=[{"id": "job-1", "kind": "smoke", "status": "succeeded"}])

    asyncio.run(job_tools.run_smoke_test(client, wait_s=5, poll_interval_s=0.01))

    assert client.enqueue_calls == [("smoke", {}, None)]


def test_get_job_status_surfaces_api_error():
    class _ErrClient:
        async def get_job(self, job_id):
            raise ZyvorApiError(404, "job not found")

    result = asyncio.run(job_tools.get_job_status(_ErrClient(), "missing-id"))

    assert result == {"error": "job not found", "status_code": 404}


def test_cancel_job_delegates_to_client():
    client = _FakeClient()

    result = asyncio.run(job_tools.cancel_job(client, "job-1"))

    assert result == {"cancel_requested": True, "job_id": "job-1"}
    assert client.cancel_calls == ["job-1"]


def test_list_job_kinds_only_returns_allowed_kinds():
    from integrations.mcp.allowlist import ALLOWED_KINDS

    result = asyncio.run(job_tools.list_job_kinds())

    assert set(result["kinds"]) == set(ALLOWED_KINDS)


class _FindingsClient:
    def __init__(self):
        self.calls = []

    async def list_findings(self, *, severity="", limit=50):
        self.calls.append((severity, limit))
        return {"findings": [], "total": 0, "counts": {}}

    async def list_schedules(self):
        return {"schedules": [{"id": "s1", "kind": "smoke"}]}


def test_get_findings_normalises_and_clamps():
    client = _FindingsClient()
    result = asyncio.run(job_tools.get_findings(client, " HIGH ", 9999))
    assert result["total"] == 0
    assert client.calls == [("high", 200)]


def test_get_findings_rejects_unknown_severity_without_calling_api():
    client = _FindingsClient()
    result = asyncio.run(job_tools.get_findings(client, "scary"))
    assert result["status_code"] == 400 and client.calls == []


def test_get_findings_and_schedules_surface_api_errors():
    class _Err:
        async def list_findings(self, **kw):
            raise ZyvorApiError(403, "missing scope")

        async def list_schedules(self):
            raise ZyvorApiError(403, "missing scope")

    assert asyncio.run(job_tools.get_findings(_Err()))["status_code"] == 403
    assert asyncio.run(job_tools.list_schedules(_Err()))["error"] == "missing scope"


def test_list_schedules_returns_api_payload():
    assert asyncio.run(job_tools.list_schedules(_FindingsClient()))["schedules"][0]["id"] == "s1"
