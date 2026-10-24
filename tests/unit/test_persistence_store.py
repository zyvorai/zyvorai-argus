# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import orchestrator.persistence.store as store_module
from orchestrator.persistence.store import MissionControlStore, _loads


def _backdate_heartbeat(store: MissionControlStore, job_id: str, seconds_ago: float) -> None:
    stale = datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (stale, job_id))


def test_job_lifecycle(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {"url": "https://zyvor.dev"}, requested_by="tester")
    assert job["status"] == "queued"
    claimed = store.claim_job()
    assert claimed and claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    store.finish_job(job["id"], result={"passed": 4})
    complete = store.get_job(job["id"])
    assert complete and complete["status"] == "succeeded"
    assert complete["result"]["passed"] == 4


def test_idempotency(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    first = store.enqueue_job("smoke", {}, idempotency_key="deploy-123")
    second = store.enqueue_job("smoke", {}, idempotency_key="deploy-123")
    assert first["id"] == second["id"]


def test_trace_context_round_trips_through_enqueue_and_claim(tmp_path):
    """Cross-replica trace propagation: a traceparent persisted at enqueue
    time (as if captured from an active job.enqueue span) survives a claim
    on what could be a different process/replica, so DurableJobService can
    parent the job.execute span on it."""
    store = MissionControlStore(tmp_path / "state.db")
    traceparent = "00-30957595af83ba0d07f0a11ce2733726-097cdd883f795456-01"
    job = store.enqueue_job("smoke", {}, trace_context=traceparent)
    assert job["trace_context"] == traceparent

    claimed = store.claim_job()
    assert claimed and claimed["trace_context"] == traceparent

    fetched = store.get_job(job["id"])
    assert fetched and fetched["trace_context"] == traceparent


def test_trace_context_defaults_to_none(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    assert job["trace_context"] is None


def test_schedule_persists_and_redacts(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    schedule = store.add_schedule(
        "realtime",
        {"token": {"$secret": "env:QA_TOKEN"}, "url": "https://zyvor.dev"},
        60,
    )
    assert schedule["params"]["token"] == "***"
    assert MissionControlStore(tmp_path / "state.db").list_schedules()[0]["id"] == schedule["id"]


def test_findings_are_deduplicated(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    first = store.add_finding("audit", "high", "Broken API", fingerprint="api:/v1/x")
    second = store.add_finding("audit", "high", "Broken API", fingerprint="api:/v1/x")
    assert first == second
    rows = store.list_findings()["findings"]
    assert rows[0]["occurrences"] == 2


def test_webhook_delivery_deduplication(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    assert store.record_webhook_delivery("d1", "push", "abc")
    assert not store.record_webhook_delivery("d1", "push", "abc")


def test_persisted_job_rejects_raw_token(tmp_path):
    import pytest
    from orchestrator.security.secrets import SecretReferenceError

    store = MissionControlStore(tmp_path / "state.db")
    with pytest.raises(SecretReferenceError):
        store.enqueue_job("realtime", {"token": "raw-secret"})


def test_recover_stale_jobs_requeues_under_attempt_cap(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()  # attempt -> 1
    _backdate_heartbeat(store, job["id"], 400)

    result = store.recover_stale_jobs(stale_after_s=300)

    assert result == {"requeued": 1, "dead_lettered": 0}
    refreshed = store.get_job(job["id"])
    assert refreshed and refreshed["status"] == "queued"


def test_recover_stale_jobs_dead_letters_at_attempt_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ZYVOR_JOB_MAX_ATTEMPTS", "2")
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()  # attempt -> 1
    store.requeue_job(job["id"])  # simulate a crashed worker, back to queued
    store.claim_job()  # attempt -> 2, at the cap
    _backdate_heartbeat(store, job["id"], 400)

    result = store.recover_stale_jobs(stale_after_s=300)

    assert result == {"requeued": 0, "dead_lettered": 1}
    dead = store.get_job(job["id"])
    assert dead and dead["status"] == "failed"
    assert "dead-lettered" in dead["error"]


# -- _loads --------------------------------------------------------------


def test_loads_returns_default_on_malformed_json():
    assert _loads("{not valid json", {"fallback": True}) == {"fallback": True}


def test_loads_returns_default_on_empty_value():
    assert _loads(None, []) == []
    assert _loads("", []) == []


# -- enqueue_job: IntegrityError edge cases -------------------------------


def test_enqueue_job_reraises_integrity_error_without_idempotency_key(tmp_path, monkeypatch):
    store = MissionControlStore(tmp_path / "state.db")
    fixed_id = uuid.UUID(int=1)
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: fixed_id)

    store.enqueue_job("smoke", {})
    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue_job("smoke", {})  # same forced id, no idempotency_key to fall back on


def test_enqueue_job_reraises_when_colliding_id_has_a_different_idempotency_key(tmp_path, monkeypatch):
    store = MissionControlStore(tmp_path / "state.db")
    fixed_id = uuid.UUID(int=2)
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: fixed_id)

    store.enqueue_job("smoke", {}, idempotency_key="key-A")
    with pytest.raises(sqlite3.IntegrityError):
        # Same forced id collides on the primary key, but the idempotency_key
        # is different -- so the fallback lookup by that key finds nothing.
        store.enqueue_job("smoke", {}, idempotency_key="key-B")


# -- claim_job -------------------------------------------------------------


def test_claim_job_returns_none_when_queue_is_empty(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    assert store.claim_job() is None


def test_claim_job_returns_none_when_the_update_loses_a_race(tmp_path, monkeypatch):
    """Simulates the belt-and-suspenders guard for the (SQLite-BEGIN-IMMEDIATE-
    should-prevent-in-practice) case where the claiming UPDATE affects zero
    rows despite the prior SELECT finding a queued job."""
    store = MissionControlStore(tmp_path / "state.db")
    store.enqueue_job("smoke", {})

    class _RowcountZero:
        def __init__(self, cursor):
            self._cursor = cursor
            self.rowcount = 0

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _ProxyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            cur = self._real.execute(sql, params)
            if sql.strip().startswith("UPDATE jobs SET status='running'"):
                return _RowcountZero(cur)
            return cur

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = store.connect

    @contextmanager
    def patched_connect():
        with real_connect() as conn:
            yield _ProxyConn(conn)

    monkeypatch.setattr(store, "connect", patched_connect)

    assert store.claim_job() is None


# -- heartbeat / cancel_job / cancellation_requested / mark_cancelled ------


def _raw_heartbeat_at(store: MissionControlStore, job_id: str) -> str:
    with store.connect() as conn:
        row = conn.execute("SELECT heartbeat_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["heartbeat_at"]


def test_heartbeat_updates_running_job_only(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()
    before = _raw_heartbeat_at(store, job["id"])

    time.sleep(0.01)
    store.heartbeat(job["id"])

    after = _raw_heartbeat_at(store, job["id"])
    assert after >= before


def test_heartbeat_does_not_touch_a_non_running_job(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})  # still queued, never claimed

    store.heartbeat(job["id"])

    with store.connect() as conn:
        row = conn.execute("SELECT heartbeat_at FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert row["heartbeat_at"] is None


def test_cancel_job_marks_a_queued_job_cancelled_immediately(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})

    assert store.cancel_job(job["id"]) is True

    refreshed = store.get_job(job["id"])
    assert refreshed["status"] == "cancelled"
    assert refreshed["cancel_requested"] is True


def test_cancel_job_flags_a_running_job_without_finishing_it(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()

    assert store.cancel_job(job["id"]) is True

    refreshed = store.get_job(job["id"])
    assert refreshed["status"] == "running"
    assert refreshed["cancel_requested"] is True
    assert store.cancellation_requested(job["id"]) is True


def test_cancel_job_returns_false_for_already_finished_job(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()
    store.finish_job(job["id"], result={"ok": True})

    assert store.cancel_job(job["id"]) is False


def test_cancel_job_returns_false_for_unknown_job(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    assert store.cancel_job("does-not-exist") is False


def test_cancellation_requested_false_for_unknown_job(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    assert store.cancellation_requested("does-not-exist") is False


def test_mark_cancelled_sets_status_and_flag(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {})
    store.claim_job()

    store.mark_cancelled(job["id"])

    refreshed = store.get_job(job["id"])
    assert refreshed["status"] == "cancelled"
    assert refreshed["cancel_requested"] is True


# -- schedules: remove / due / advance --------------------------------------


def test_remove_schedule_returns_true_then_false(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    schedule = store.add_schedule("smoke", {}, 60)

    assert store.remove_schedule(schedule["id"]) is True
    assert store.remove_schedule(schedule["id"]) is False


def test_due_schedules_returns_only_past_due_enabled_ones(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    due_soon = store.add_schedule("smoke", {"token": {"$secret": "env:X"}}, 30)
    store.add_schedule("audit", {}, 86_400)  # far in the future, not due

    with store.connect() as conn:
        conn.execute("UPDATE schedules SET next_at=? WHERE id=?", (time.time() - 1, due_soon["id"]))

    due = store.due_schedules()

    assert [s["id"] for s in due] == [due_soon["id"]]
    # due_schedules reveals real params (the scheduler loop needs the actual
    # secret reference to enqueue the real job), unlike list_schedules.
    assert due[0]["params"]["token"] == {"$secret": "env:X"}


def test_advance_schedule_bumps_runs_when_ran(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    schedule = store.add_schedule("smoke", {}, 60)

    store.advance_schedule(schedule["id"], ran=True)

    refreshed = store.get_schedule(schedule["id"])
    assert refreshed["runs"] == 1
    assert refreshed["last_at"] is not None


def test_advance_schedule_does_not_bump_runs_when_not_ran(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    schedule = store.add_schedule("smoke", {}, 60)

    store.advance_schedule(schedule["id"], ran=False)

    refreshed = store.get_schedule(schedule["id"])
    assert refreshed["runs"] == 0
    assert refreshed["last_at"] is None


def test_advance_schedule_is_a_no_op_for_unknown_schedule(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    store.advance_schedule("does-not-exist", ran=True)  # must not raise


def test_advance_schedule_catch_up_preserves_cadence(tmp_path, monkeypatch):
    monkeypatch.setenv("ZYVOR_SCHEDULE_CATCHUP", "true")
    store = MissionControlStore(tmp_path / "state.db")
    schedule = store.add_schedule("smoke", {}, 60)
    overdue = time.time() - 120
    with store.connect() as conn:
        conn.execute("UPDATE schedules SET next_at=? WHERE id=?", (overdue, schedule["id"]))
    store.advance_schedule(schedule["id"], ran=True)
    with store.connect() as conn:
        nxt = conn.execute("SELECT next_at FROM schedules WHERE id=?", (schedule["id"],)).fetchone()["next_at"]
    assert abs(nxt - (overdue + 60)) < 0.5
    refreshed = store.get_schedule(schedule["id"])
    assert refreshed["runs"] == 1


# -- audit -------------------------------------------------------------------


def test_audit_records_and_lists_events(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    store.audit(
        "job.enqueue", actor="alice", resource_type="job", resource_id="job-1",
        detail={"kind": "smoke", "token": {"$secret": "env:X"}},
    )

    events = store.list_audit()

    assert len(events) == 1
    assert events[0]["action"] == "job.enqueue"
    assert events[0]["actor"] == "alice"
    assert events[0]["detail"]["kind"] == "smoke"


def test_list_audit_respects_limit_and_ordering(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    for i in range(3):
        store.audit(f"action-{i}")

    events = store.list_audit(limit=2)

    assert len(events) == 2
    assert events[0]["action"] == "action-2"  # most recent first


# -- record_webhook_delivery -------------------------------------------------


def test_record_webhook_delivery_rejects_empty_delivery_id(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="X-GitHub-Delivery is required"):
        store.record_webhook_delivery("", "push", "abc")


def test_callback_url_is_stored_but_never_returned(tmp_path):
    store = MissionControlStore(tmp_path / "state.db")
    job = store.enqueue_job("smoke", {}, callback_url="https://hooks.example.com/x?token=TOPSECRET")

    assert store.get_job_callback(job["id"]) == "https://hooks.example.com/x?token=TOPSECRET"
    for view in (job, store.get_job(job["id"], reveal_params=True), *store.list_jobs()):
        assert "TOPSECRET" not in repr(view)
        assert "callback_url" not in view
    assert store.get_job_callback("missing") is None
    plain = store.enqueue_job("smoke", {})
    assert store.get_job_callback(plain["id"]) is None


def test_callback_column_is_added_to_a_pre_existing_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    MissionControlStore(path)  # create current schema
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN callback_url")
        conn.execute("UPDATE schema_meta SET version=6")
    store = MissionControlStore(path)  # re-open runs the migration
    job = store.enqueue_job("smoke", {}, callback_url="https://hooks.example.com/x")
    assert store.get_job_callback(job["id"]) == "https://hooks.example.com/x"
