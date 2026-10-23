# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Concurrent jobs: safe kinds may run beside the slot job when
ZYVOR_JOBS_MAX_CONCURRENT > 1; everything else stays serialized, and each job
keeps its own progress log and cancel flag."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from orchestrator.dashboard import durable_jobs, jobs


def _reset() -> None:
    with jobs._lock:
        jobs._progress.clear()
        jobs._live_cases.clear()
        jobs._side_jobs.clear()
        jobs._slot_history.clear()
        jobs._state.update(
            running=False, job_id=None, kind=None, params={}, started_at=None,
            finished_at=None, result=None, error=None,
        )
    jobs._cancel.clear()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ZYVOR_JOBS_MAX_CONCURRENT", raising=False)
    _reset()
    yield
    _reset()


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Blocker:
    """A fake job that logs, then waits (honouring cancel) until released."""

    def __init__(self, tag: str):
        self.tag = tag
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, params):
        jobs.log_progress(f"{self.tag} working")
        self.started.set()
        while not self.release.wait(0.01):
            jobs._check_cancel()
        return {"tag": self.tag}


def _install(monkeypatch, **fakes):
    monkeypatch.setattr(jobs, "_JOBS", {**jobs._JOBS, **fakes})
    monkeypatch.setattr(jobs, "_validate", lambda kind, params: dict(params))


def test_default_allows_one_job_at_a_time(monkeypatch):
    slow = _Blocker("smoke")
    _install(monkeypatch, smoke=slow, ping=_Blocker("ping"))
    assert jobs.trigger("smoke")[0] is True
    assert slow.started.wait(2)
    started, _ = jobs.trigger("ping")
    assert started is False
    slow.release.set()
    assert _wait(lambda: not jobs.status()["running"])


def test_safe_kind_runs_beside_the_slot_job_when_enabled(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "3")
    slot, probe = _Blocker("smoke"), _Blocker("ping")
    _install(monkeypatch, smoke=slot, ping=probe)

    assert jobs.trigger("smoke")[0] is True
    assert slot.started.wait(2)
    started, side_state = jobs.trigger("ping")
    assert started is True
    assert side_state["job_id"] and side_state["concurrent"] is True
    assert probe.started.wait(2)

    # Each job's log stays on its own job.
    slot_log = " ".join(jobs.status()["progress"])
    side_log = " ".join(jobs.status(side_state["job_id"])["progress"])
    assert "smoke working" in slot_log and "ping working" not in slot_log
    assert "ping working" in side_log and "smoke working" not in side_log

    rows = jobs.list_jobs()
    assert {r["kind"] for r in rows} == {"smoke", "ping"}
    assert sum(1 for r in rows if r["running"]) == 2

    slot.release.set()
    probe.release.set()
    assert _wait(lambda: not any(r["running"] for r in jobs.list_jobs()))
    assert jobs.status(side_state["job_id"])["result"] == {"tag": "ping"}


def test_unsafe_kind_is_rejected_even_with_capacity(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "4")
    slot = _Blocker("smoke")
    _install(monkeypatch, smoke=slot, full=_Blocker("full"))
    assert jobs.trigger("smoke")[0] is True
    assert slot.started.wait(2)
    assert jobs.trigger("full")[0] is False  # uses the Playwright runner: must serialize
    slot.release.set()
    assert _wait(lambda: not jobs.status()["running"])


def test_capacity_is_enforced(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "2")
    slot, a, b = _Blocker("smoke"), _Blocker("ping"), _Blocker("tls")
    _install(monkeypatch, smoke=slot, ping=a, tls=b)
    assert jobs.trigger("smoke")[0] is True
    assert slot.started.wait(2)
    assert jobs.trigger("ping")[0] is True
    assert jobs.trigger("tls")[0] is False  # 2 already running
    for blocker in (slot, a, b):
        blocker.release.set()
    assert _wait(lambda: not any(r["running"] for r in jobs.list_jobs()))


def test_cancel_by_id_stops_only_that_job(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "3")
    slot, probe = _Blocker("smoke"), _Blocker("ping")
    _install(monkeypatch, smoke=slot, ping=probe)
    terminated = []
    import agents.execution.runner as runner

    monkeypatch.setattr(runner, "terminate_current", lambda: terminated.append(1) or True)

    assert jobs.trigger("smoke")[0] is True
    assert slot.started.wait(2)
    _, side_state = jobs.trigger("ping")
    assert probe.started.wait(2)

    jobs.cancel(side_state["job_id"])
    assert _wait(lambda: jobs.status(side_state["job_id"])["error"] == "cancelled by user")
    assert jobs.status()["running"] is True  # the slot job is untouched
    assert not jobs._cancel.is_set()
    assert terminated == []  # the Playwright process belongs to the slot job

    slot.release.set()
    assert _wait(lambda: not jobs.status()["running"])


def test_cancel_with_unknown_id_never_cancels_the_slot_job(monkeypatch):
    slot = _Blocker("smoke")
    _install(monkeypatch, smoke=slot)
    assert jobs.trigger("smoke")[0] is True
    assert slot.started.wait(2)
    state = jobs.cancel("does-not-exist")
    assert state["error"] == "unknown job"
    assert not jobs._cancel.is_set()
    assert jobs.status()["running"] is True
    slot.release.set()
    assert _wait(lambda: not jobs.status()["running"])


def test_cancel_without_id_still_cancels_the_slot_job(monkeypatch):
    slot = _Blocker("smoke")
    _install(monkeypatch, smoke=slot)
    jobs.trigger("smoke")
    assert slot.started.wait(2)
    jobs.cancel()
    assert _wait(lambda: jobs.status()["error"] == "cancelled by user")


def test_finished_slot_job_stays_readable_by_id(monkeypatch):
    quick = MagicMock(return_value={"ok": 1})
    _install(monkeypatch, smoke=quick)
    _, state = jobs.trigger("smoke")
    job_id = state["job_id"]
    assert _wait(lambda: not jobs.status()["running"])
    # Another job takes the slot; the first one's result is still reachable.
    _install(monkeypatch, smoke=MagicMock(return_value={"ok": 2}))
    jobs.trigger("smoke")
    assert _wait(lambda: not jobs.status()["running"])
    assert jobs.status(job_id)["result"] == {"ok": 1}


def test_unknown_job_status_is_reported_not_guessed():
    state = jobs.status("nope")
    assert state["error"] == "unknown job" and state["running"] is False


def test_finished_concurrent_jobs_are_pruned(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "2")
    monkeypatch.setattr(jobs, "_MAX_FINISHED_SIDE_JOBS", 3)
    slot = _Blocker("smoke")
    _install(monkeypatch, smoke=slot, ping=MagicMock(return_value={}))
    jobs.trigger("smoke")
    assert slot.started.wait(2)
    for _ in range(8):
        assert _wait(lambda: jobs.trigger("ping")[0])
        assert _wait(lambda: not any(j.running for j in jobs._side_jobs.values()))
    assert len([j for j in jobs._side_jobs.values() if not j.running]) <= 4
    slot.release.set()


def test_max_concurrent_parses_and_clamps(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "abc")
    assert jobs.max_concurrent() == 1
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "0")
    assert jobs.max_concurrent() == 1
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "99")
    assert jobs.max_concurrent() == 16


def test_current_logger_follows_the_job_into_other_threads(monkeypatch):
    monkeypatch.setenv("ZYVOR_JOBS_MAX_CONCURRENT", "2")
    slot = _Blocker("smoke")
    done = threading.Event()

    def threaded_probe(params):
        log = jobs.current_logger()
        t = threading.Thread(target=lambda: (log("from child thread"), done.set()))
        t.start()
        t.join()
        return {}

    _install(monkeypatch, smoke=slot, ping=threaded_probe)
    jobs.trigger("smoke")
    assert slot.started.wait(2)
    _, side_state = jobs.trigger("ping")
    assert done.wait(2)
    assert any("from child thread" in line for line in jobs.status(side_state["job_id"])["progress"])
    assert not any("from child thread" in line for line in jobs.status()["progress"])
    slot.release.set()


class _Stop:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, _t):
        return self._set


def test_durable_worker_follows_its_own_job_id(monkeypatch):
    store = MagicMock()
    service = durable_jobs.DurableJobService(store=store, poll_s=0.01)
    service._stop = _Stop()
    calls = {"n": 0}

    def claim():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": "durable-1", "kind": "ping", "params": {}}
        service._stop.set()
        return None

    store.claim_job.side_effect = claim
    store.cancellation_requested.return_value = False
    seen = []
    monkeypatch.setattr(jobs, "trigger", lambda kind, params: (True, {"job_id": "abc123"}))

    def fake_status(job_id=None):
        seen.append(job_id)
        return {"running": False, "error": None, "result": {"ok": True}}

    monkeypatch.setattr(jobs, "status", fake_status)
    service._worker_loop()
    assert seen == ["abc123"]  # polled its own job, not "whatever is current"
    store.finish_job.assert_called_once_with("durable-1", result={"ok": True}, error=None)
