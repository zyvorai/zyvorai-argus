# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""db_migration_check: validation, the runner's suite mode (real subprocess
against real SQLite), the baseline comparison, and the job flow with a fake
sandbox. Argus never runs the migration, so these tests only ever issue
SELECTs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import orchestrator.dashboard.findings as findings_module
import orchestrator.dashboard.history as history_module
import orchestrator.dashboard.jobs as jobs_module
import orchestrator.persistence.store as store_module
import orchestrator.security.sandbox as sandbox_module
from agents.db_assert.migration import compare_to_baseline, parse_check_lines
from orchestrator.dashboard.jobs import VALID_KINDS, _validate

RUNNER = Path(__file__).resolve().parents[2] / "agents" / "db_assert" / "runner_script.py"

class _EngagementStore:
    def __init__(self, engagement):
        self._engagement = engagement

    def get_engagement(self, engagement_id):
        return self._engagement if engagement_id == "eng-1" else None

    def audit(self, action, **kwargs):
        pass


def _allow_engagement(monkeypatch):
    engagement = {
        "id": "eng-1", "target_pattern": "*", "scope_statement": "authorized", "tier": "active_recon",
        "authorized_by": "admin", "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": None, "revoked_at": None, "revoked_by": None,
    }
    monkeypatch.setattr(store_module, "get_store", lambda: _EngagementStore(engagement))


CHECKS = [
    {"name": "orders-exist", "query": "SELECT id FROM orders", "assertion": {"mode": "row_count", "op": ">=", "value": 1}},
    {"name": "orders-stable", "query": "SELECT id, status FROM orders ORDER BY id", "stable": True},
]


def _params(**over):
    p = {
        "engine": "sqlite", "target": "orders-db", "db_secret": {"$secret": "env:DB_DSN"},
        "phase": "pre", "migration": "add-index", "checks": CHECKS, "engagement_id": "eng-1",
    }
    p.update(over)
    return p


# -- validation --------------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ZYVOR_DB_TESTING_ENABLED", "true")
    _allow_engagement(monkeypatch)


def test_registered():
    assert "db_migration_check" in VALID_KINDS


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ZYVOR_DB_TESTING_ENABLED", raising=False)
    with pytest.raises(ValueError, match="db_migration_check is disabled"):
        _validate("db_migration_check", _params())


def test_clean_pre(enabled):
    clean = _validate("db_migration_check", _params())
    assert clean["phase"] == "pre"
    assert clean["checks"][1]["stable"] is True
    assert clean["checks"][0]["stable"] is False
    assert clean["timeout_s"] == 30


@pytest.mark.parametrize("over,match", [
    ({"phase": "during"}, "phase"),
    ({"migration": ""}, "migration"),
    ({"checks": []}, "checks"),
    ({"checks": [{"name": "a", "query": "DELETE FROM orders"}]}, "SELECT"),
    ({"checks": [{"name": "a", "query": "SELECT 1"}, {"name": "a", "query": "SELECT 2"}]}, "unique"),
    ({"checks": [{"name": "a", "query": "SELECT 1", "assertion": {"mode": "bogus"}}]}, "assertion"),
    ({"checks": [{"name": "c%d" % i, "query": "SELECT 1"} for i in range(26)]}, "25"),
    ({"phase": "post"}, "baseline_job_id"),
])
def test_rejects(enabled, over, match):
    with pytest.raises(ValueError, match=match):
        _validate("db_migration_check", _params(**over))


def test_post_keeps_baseline_id(enabled):
    clean = _validate("db_migration_check", _params(phase="post", baseline_job_id="job-1"))
    assert clean["baseline_job_id"] == "job-1"


# -- comparison ---------------------------------------------------------------

def _entry(name, digest="d1", columns=("id",), rows=1):
    return {"name": name, "ok": True, "digest": digest, "columns": list(columns), "rows": rows}


def test_no_drift_when_unchanged():
    assert compare_to_baseline([_entry("a")], [_entry("a")], {"a"}) == []


def test_data_change_is_drift_only_when_stable():
    changed = [_entry("a", digest="d2", rows=2)]
    assert compare_to_baseline(changed, [_entry("a")], set()) == []
    drift = compare_to_baseline(changed, [_entry("a")], {"a"})
    assert [d["kind"] for d in drift] == ["data"]


def test_column_change_and_missing_check_are_always_drift():
    drift = compare_to_baseline([_entry("a", columns=("id", "x"))], [_entry("a"), _entry("b")], set())
    assert sorted(d["kind"] for d in drift) == ["columns", "missing"]


def test_parse_check_lines_ignores_noise():
    out = 'noise\nCHECK: {"name": "a", "ok": true}\nCHECK: not-json\nVERIFIED: true - 1/1 checks passed\n'
    assert [c["name"] for c in parse_check_lines(out)] == ["a"]


# -- runner suite mode: real subprocess, real SQLite ---------------------------

def _db(tmp_path, rows):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def _run_suite(path, checks):
    env = {**os.environ, "ZYVOR_DB_ENGINE": "sqlite", "ZYVOR_DB_SECRET": path, "ZYVOR_DB_CHECKS": json.dumps(checks)}
    return subprocess.run([sys.executable, str(RUNNER)], env=env, capture_output=True, text=True, timeout=15)


def test_runner_suite_reports_each_check_and_never_prints_rows(tmp_path):
    path = _db(tmp_path, [(1, "paid-secret-value"), (2, "open")])
    proc = _run_suite(path, [
        {"name": "has-rows", "query": "SELECT id FROM orders", "assertion": {"mode": "row_count", "op": "==", "value": 2}},
        {"name": "too-many", "query": "SELECT id FROM orders", "assertion": {"mode": "row_count", "op": "==", "value": 9}},
        {"name": "broken", "query": "SELECT nope FROM orders"},
    ])
    checks = parse_check_lines(proc.stdout)
    assert [(c["name"], c["ok"]) for c in checks] == [("has-rows", True), ("too-many", False), ("broken", False)]
    assert checks[0]["rows"] == 2 and checks[0]["columns"] == ["id"] and len(checks[0]["digest"]) == 64
    assert "VERIFIED: false - 1/3 checks passed" in proc.stdout
    assert "paid-secret-value" not in proc.stdout


def test_runner_digest_changes_when_data_changes(tmp_path):
    check = [{"name": "o", "query": "SELECT id, status FROM orders ORDER BY id"}]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = parse_check_lines(_run_suite(_db(tmp_path / "a", [(1, "a")]), check).stdout)[0]
    same = parse_check_lines(_run_suite(_db(tmp_path / "b", [(1, "a")]), check).stdout)[0]
    (tmp_path / "c").mkdir()
    b = parse_check_lines(_run_suite(_db(tmp_path / "c", [(1, "b")]), check).stdout)[0]
    assert a["digest"] == same["digest"]
    assert a["digest"] != b["digest"]


def test_runner_rejects_malformed_checks_json():
    env = {**os.environ, "ZYVOR_DB_ENGINE": "sqlite", "ZYVOR_DB_SECRET": ":memory:", "ZYVOR_DB_CHECKS": "{oops"}
    proc = subprocess.run([sys.executable, str(RUNNER)], env=env, capture_output=True, text=True, timeout=15)
    assert "VERIFIED: false - malformed checks JSON" in proc.stdout


# -- job flow with a fake sandbox ---------------------------------------------

class _Store:
    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.audits = []

    def audit(self, *a, **kw):
        self.audits.append((a, kw))

    def get_job(self, job_id, **kw):
        return self.jobs.get(job_id)


def _stdout(entries):
    lines = [f"CHECK: {json.dumps(e)}" for e in entries]
    bad = sum(1 for e in entries if not e["ok"])
    lines.append(f"VERIFIED: {'false' if bad else 'true'} - {len(entries) - bad}/{len(entries)} checks passed")
    return "\n".join(lines) + "\n"


def _patch(monkeypatch, store, stdout):
    monkeypatch.setattr(history_module, "append_run", lambda *a, **kw: None)
    monkeypatch.setattr(store_module, "get_store", lambda: store)
    monkeypatch.setattr(sandbox_module, "available", lambda: True)
    monkeypatch.setattr(sandbox_module, "db_image", lambda: "img")
    added = []
    monkeypatch.setattr(findings_module, "add", lambda *a, **kw: added.append((a, kw)))
    monkeypatch.setattr(
        sandbox_module, "run_python",
        lambda code, **kw: sandbox_module.SandboxResult(exit_code=0, stdout=stdout, timed_out=False, network_policy_applied=False),
    )
    monkeypatch.setenv("DB_DSN", "sqlite:///secret-dsn")
    return added


def _job_params(**over):
    p = {
        "engine": "sqlite", "target": "orders-db", "db_secret": {"$secret": "env:DB_DSN"}, "timeout_s": 30,
        "phase": "pre", "migration": "add-index", "checks": [
            {**CHECKS[0], "query_params": [], "stable": False},
            {**CHECKS[1], "query_params": [], "assertion": None, "stable": True},
        ],
    }
    p.update(over)
    return p


PRE = [_entry("orders-exist"), _entry("orders-stable")]


def test_pre_run_returns_snapshot_and_passes(monkeypatch):
    store = _Store()
    _patch(monkeypatch, store, _stdout(PRE))
    result = jobs_module._job_db_migration_check(_job_params())
    assert result["passed"] is True and [c["name"] for c in result["checks"]] == ["orders-exist", "orders-stable"]
    assert "secret-dsn" not in str(result) and "secret-dsn" not in str(store.audits)


def test_post_flags_stable_data_drift(monkeypatch):
    base = {"kind": "db_migration_check", "result": {"phase": "pre", "migration": "add-index", "target": "orders-db", "checks": PRE}}
    store = _Store({"job-1": base})
    post = [_entry("orders-exist"), _entry("orders-stable", digest="CHANGED", rows=0)]
    added = _patch(monkeypatch, store, _stdout(post))
    result = jobs_module._job_db_migration_check(_job_params(phase="post", baseline_job_id="job-1"))
    assert result["passed"] is False
    assert [d["kind"] for d in result["drift"]] == ["data"]
    assert added and added[0][1]["category"] == "db-migration-drift"


def test_post_passes_when_only_unstable_check_changes(monkeypatch):
    base = {"kind": "db_migration_check", "result": {"phase": "pre", "migration": "add-index", "target": "orders-db", "checks": PRE}}
    store = _Store({"job-1": base})
    post = [_entry("orders-exist", digest="CHANGED", rows=5), _entry("orders-stable")]
    _patch(monkeypatch, store, _stdout(post))
    assert jobs_module._job_db_migration_check(_job_params(phase="post", baseline_job_id="job-1"))["passed"] is True


def test_post_rejects_wrong_baseline(monkeypatch):
    store = _Store({"job-1": {"kind": "db_assert", "result": {}}})
    _patch(monkeypatch, store, _stdout(PRE))
    with pytest.raises(ValueError, match="baseline_job_id"):
        jobs_module._job_db_migration_check(_job_params(phase="post", baseline_job_id="job-1"))
    with pytest.raises(ValueError, match="baseline_job_id"):
        jobs_module._job_db_migration_check(_job_params(phase="post", baseline_job_id="missing"))


def test_failed_invariant_and_incomplete_suite_fail(monkeypatch):
    store = _Store()
    bad = [{**_entry("orders-exist"), "ok": False, "reason": "row_count 0 does not satisfy >= 1"}]
    added = _patch(monkeypatch, store, _stdout(bad))  # only 1 of 2 checks reported
    result = jobs_module._job_db_migration_check(_job_params())
    assert result["passed"] is False
    cats = {a[1]["category"] for a in added}
    assert cats == {"db-migration-invariant-failed", "db-migration-suite-incomplete"}
