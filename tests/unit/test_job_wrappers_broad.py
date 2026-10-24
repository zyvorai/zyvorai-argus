# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Broad registry + thin-wrapper coverage for `_job_*` handlers.

Deep behavior stays in dedicated modules (test_db_assert_job, test_chaos_jobs,
…). This file asserts every `_JOBS` entry is callable and exercises a
representative set of wrappers with network/pipeline mocked out.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import orchestrator.dashboard.history as history_module
import orchestrator.dashboard.jobs as jobs_module


@pytest.fixture(autouse=True)
def _quiet_history(monkeypatch):
    monkeypatch.setattr(history_module, "append_run", lambda *a, **kw: None)
    monkeypatch.setattr(jobs_module, "log_progress", lambda *a, **kw: None)
    monkeypatch.setattr(jobs_module, "_check_cancel", lambda: None)


def test_every_registered_job_is_callable():
    assert jobs_module._JOBS, "_JOBS must not be empty"
    for kind, fn in jobs_module._JOBS.items():
        assert callable(fn), kind


def test_job_registry_covers_core_kinds():
    for kind in (
        "smoke", "full", "generate", "ping", "tls", "loadtest", "flow",
        "api_contract", "cve_lookup", "db_assert", "chaos_inject",
    ):
        assert kind in jobs_module._JOBS


def test_job_ping_aggregates_http_statuses(monkeypatch):
    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp(200 if "ok" in url else 503)

    monkeypatch.setattr(httpx, "Client", _Client)
    result = jobs_module._job_ping(
        {"urls": ["https://ok.example/", "https://bad.example/"], "insecure": False}
    )
    assert result["total"] == 2
    assert result["up"] == 1
    assert result["down"] == 1


def test_job_tls_reports_days_left(monkeypatch):
    import socket
    import ssl

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getpeercert(self):
            return {
                "issuer": ((("commonName", "Test CA"),),),
                "subject": ((("commonName", "example.com"),),),
                "notAfter": "Dec 31 23:59:59 2099 GMT",
                "subjectAltName": (("DNS", "example.com"),),
            }

        def version(self):
            return "TLSv1.3"

        def cipher(self):
            return ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            return _Sock()

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("1.2.3.4", 443))])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _Conn())
    monkeypatch.setattr(ssl, "create_default_context", lambda: _Ctx())

    result = jobs_module._job_tls({"host": "example.com", "port": 443})
    assert result["host"] == "example.com"
    assert result["status"] == "ok"
    assert result["days_left"] is not None and result["days_left"] > 0


def test_job_cve_lookup_delegates(monkeypatch):
    monkeypatch.setattr(
        "agents.probes.cve_lookup.run_cve_lookup",
        lambda url, insecure=False, log=None: {
            "total_matches": 0,
            "identified": [],
            "matches": [],
        },
    )
    monkeypatch.setattr(jobs_module, "_auto_findings", lambda *a, **kw: [])
    result = jobs_module._job_cve_lookup({"url": "https://example.com/", "insecure": False})
    assert result["url"] == "https://example.com/"
    assert result["total_matches"] == 0


def test_job_smoke_delegates_to_playwright(monkeypatch):
    fake = SimpleNamespace(passed=2, failed=0, total=2, cases=[])
    monkeypatch.setattr(
        "agents.execution.runner.run_playwright",
        lambda **kw: fake,
    )
    monkeypatch.setattr(jobs_module, "_persist_artifacts", lambda *a, **kw: ([], []))
    monkeypatch.setattr(
        jobs_module,
        "_finalize",
        lambda *a, **kw: {"videos": [], "traces": []},
    )
    monkeypatch.setattr("agents.reporter.agent.generate_summary_stub", lambda r: "ok")
    result = jobs_module._job_smoke({})
    assert result["passed"] == 2
    assert result["failed"] == 0
    assert result["total"] == 2


def test_job_port_scan_delegates(monkeypatch):
    import agents.probes.port_scan as port_mod

    monkeypatch.setattr(
        port_mod,
        "run_port_scan",
        lambda url, ports=None, timeout_s=1.0, log=None: {
            "open_ports": [443],
            "host": "example.com",
        },
    )
    monkeypatch.setattr(jobs_module, "_auto_findings", lambda *a, **kw: [])
    result = jobs_module._job_port_scan({"url": "https://example.com/", "ports": "80,443"})
    assert result["open_ports"] == [443]
    assert result["url"] == "https://example.com/"
