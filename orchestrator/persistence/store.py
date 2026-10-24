# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""SQLite persistence for jobs, schedules, findings, audit and webhook replay protection.

SQLite is the zero-dependency default. The repository interface is deliberately
small so a PostgreSQL implementation can replace it without changing API routes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from orchestrator.security.redaction import redact
from orchestrator.security.secrets import assert_persistable

_SCHEMA_VERSION = 7

DEFAULT_MAX_JOB_ATTEMPTS = 3


def _max_job_attempts() -> int:
    return max(1, int(os.environ.get("ZYVOR_JOB_MAX_ATTEMPTS", DEFAULT_MAX_JOB_ATTEMPTS)))


def _iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp or time.time(), timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class MissionControlStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.environ.get("ZYVOR_STATE_DB") or "reports/mission-control.db"
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.Lock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        with self._migration_lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(version)
                SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
                    priority INTEGER NOT NULL DEFAULT 100,
                    idempotency_key TEXT,
                    requested_by TEXT,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    trace_context TEXT,
                    callback_url TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_uq
                    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS jobs_status_priority_idx
                    ON jobs(status, priority, queued_at);

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    interval_s INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    last_at TEXT,
                    runs INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT
                );
                CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules(enabled, next_at);

                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT,
                    url TEXT,
                    location TEXT,
                    category TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS findings_fingerprint_uq
                    ON findings(fingerprint) WHERE fingerprint IS NOT NULL;
                CREATE INDEX IF NOT EXISTS findings_status_severity_idx
                    ON findings(status, severity, last_seen);

                CREATE TABLE IF NOT EXISTS engagements (
                    id TEXT PRIMARY KEY,
                    target_pattern TEXT NOT NULL,
                    scope_statement TEXT NOT NULL,
                    tier TEXT NOT NULL CHECK(tier IN ('active_recon','exploit')),
                    authorized_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    revoked_by TEXT
                );
                CREATE INDEX IF NOT EXISTS engagements_target_idx
                    ON engagements(target_pattern, revoked_at, expires_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    actor TEXT,
                    action TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    outcome TEXT NOT NULL,
                    detail_json TEXT,
                    client_ip TEXT,
                    request_id TEXT
                );
                CREATE INDEX IF NOT EXISTS audit_at_idx ON audit_events(at DESC);

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event TEXT,
                    received_at REAL NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS webhook_received_idx ON webhook_deliveries(received_at);

                CREATE TABLE IF NOT EXISTS requirements (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    origin_id TEXT,
                    title TEXT NOT NULL,
                    latest_version INTEGER NOT NULL DEFAULT 1,
                    quality_score REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requirements_source_idx
                    ON requirements(source_type, updated_at DESC);

                CREATE TABLE IF NOT EXISTS requirement_versions (
                    requirement_id TEXT NOT NULL REFERENCES requirements(id),
                    version INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    quality_score REAL,
                    quality_issues_json TEXT,
                    created_at TEXT NOT NULL,
                    data_models_json TEXT,
                    flows_json TEXT,
                    model_dependencies_json TEXT,
                    PRIMARY KEY (requirement_id, version)
                );

                CREATE TABLE IF NOT EXISTS requirement_test_links (
                    requirement_id TEXT NOT NULL REFERENCES requirements(id),
                    requirement_version INTEGER NOT NULL,
                    test_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (requirement_id, requirement_version, test_path)
                );
                CREATE INDEX IF NOT EXISTS requirement_test_links_req_idx
                    ON requirement_test_links(requirement_id, requirement_version);
                """
            )
            # `CREATE TABLE IF NOT EXISTS` above is a no-op against a findings
            # table that already existed pre-v2 (schema_meta.version < 2) — add
            # the new column explicitly for those databases.
            existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(findings)")}
            if "category" not in existing_columns:
                conn.execute("ALTER TABLE findings ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            # Same story for jobs.trace_context, added in schema v4 (cross-replica
            # trace propagation -- see orchestrator/observability/tracing.py).
            existing_job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "trace_context" not in existing_job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN trace_context TEXT")
            # jobs.callback_url (schema v7): where to POST a completion notice.
            # Kept out of _job() on purpose -- a URL can carry a token in its
            # query string, so it is never returned by the API or audited.
            if "callback_url" not in existing_job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN callback_url TEXT")
            # Same story for requirement_versions.{data_models,flows}_json, added
            # in schema v5 (agents/requirement_entities/ -- impact analysis by
            # shared data model / business flow).
            existing_version_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(requirement_versions)")
            }
            if "data_models_json" not in existing_version_columns:
                conn.execute("ALTER TABLE requirement_versions ADD COLUMN data_models_json TEXT")
            if "flows_json" not in existing_version_columns:
                conn.execute("ALTER TABLE requirement_versions ADD COLUMN flows_json TEXT")
            if "model_dependencies_json" not in existing_version_columns:
                conn.execute(
                    "ALTER TABLE requirement_versions ADD COLUMN model_dependencies_json TEXT"
                )
            conn.execute("UPDATE schema_meta SET version=?", (_SCHEMA_VERSION,))

    # Jobs -----------------------------------------------------------------
    def enqueue_job(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        requested_by: str = "",
        priority: int = 100,
        idempotency_key: str | None = None,
        trace_context: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        assert_persistable(params)
        now = _iso()
        job_id = str(uuid.uuid4())
        with self.connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO jobs
                    (id, kind, params_json, status, priority, idempotency_key, requested_by, queued_at,
                     trace_context, callback_url)
                    VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                    (job_id, kind, _json(params), int(priority), idempotency_key, requested_by, now,
                     trace_context, callback_url),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row is None:
                    raise
                return self._job(row)
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row)

    def claim_job(self) -> dict[str, Any] | None:
        """Atomically claim the next queued job."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY priority ASC, queued_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = _iso()
            changed = conn.execute(
                """UPDATE jobs SET status='running', started_at=?, heartbeat_at=?, attempt=attempt+1
                WHERE id=? AND status='queued'""",
                (now, now, row["id"]),
            ).rowcount
            conn.execute("COMMIT")
            if changed != 1:
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return self._job(claimed, reveal_params=True)

    def heartbeat(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? AND status='running'", (_iso(), job_id))

    def requeue_job(self, job_id: str, error: str = "worker busy") -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE jobs SET status='queued', started_at=NULL, heartbeat_at=NULL, error=?
                WHERE id=? AND status='running'""",
                (str(redact(error)), job_id),
            )

    def finish_job(self, job_id: str, *, result: Any = None, error: str | None = None) -> None:
        status = "failed" if error else "succeeded"
        safe_result = redact(result) if result is not None else None
        safe_error = redact(error) if error is not None else None
        with self.connect() as conn:
            conn.execute(
                """UPDATE jobs SET status=?, result_json=?, error=?, finished_at=?, heartbeat_at=?
                WHERE id=?""",
                (status, _json(safe_result) if safe_result is not None else None, safe_error, _iso(), _iso(), job_id),
            )

    def cancel_job(self, job_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled"}:
                return False
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=? WHERE id=?",
                    (_iso(), job_id),
                )
            else:
                conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            return True

    def cancellation_requested(self, job_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def mark_cancelled(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, cancel_requested=1 WHERE id=?",
                (_iso(), job_id),
            )

    def recover_stale_jobs(self, stale_after_s: int = 300) -> dict[str, int]:
        """Requeue jobs stuck past a heartbeat timeout — unless they've already
        exhausted their attempt budget, in which case dead-letter them (mark
        `failed`) instead of requeuing forever."""
        cutoff = _iso(time.time() - stale_after_s)
        max_attempts = _max_job_attempts()
        with self.connect() as conn:
            dead_lettered = conn.execute(
                """UPDATE jobs SET status='failed', finished_at=?, heartbeat_at=?,
                error='dead-lettered: exceeded ' || ? || ' attempts (stale worker heartbeat)'
                WHERE status='running' AND attempt>=? AND COALESCE(heartbeat_at, started_at) < ?""",
                (_iso(), _iso(), max_attempts, max_attempts, cutoff),
            ).rowcount
            requeued = conn.execute(
                """UPDATE jobs SET status='queued', started_at=NULL, heartbeat_at=NULL,
                error='recovered after stale worker heartbeat'
                WHERE status='running' AND attempt<? AND COALESCE(heartbeat_at, started_at) < ?""",
                (max_attempts, cutoff),
            ).rowcount
        return {"requeued": requeued, "dead_lettered": dead_lettered}

    def get_job(self, job_id: str, *, reveal_params: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row, reveal_params=reveal_params) if row else None

    def get_job_callback(self, job_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT callback_url FROM jobs WHERE id=?", (job_id,)).fetchone()
        return (row["callback_url"] or None) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY queued_at DESC LIMIT ?", (min(limit, 500),)).fetchall()
        return [self._job(row) for row in rows]

    def _job(self, row: sqlite3.Row, *, reveal_params: bool = False) -> dict[str, Any]:
        params = _loads(row["params_json"], {})
        return {
            "id": row["id"],
            "kind": row["kind"],
            "params": params if reveal_params else redact(params),
            "status": row["status"],
            "priority": row["priority"],
            "requested_by": row["requested_by"],
            "queued_at": row["queued_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result": redact(_loads(row["result_json"], None)),
            "error": redact(row["error"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "attempt": row["attempt"],
            "trace_context": row["trace_context"],
        }

    # Schedules ------------------------------------------------------------
    def add_schedule(
        self,
        kind: str,
        params: dict[str, Any],
        interval_s: int,
        *,
        requested_by: str = "",
    ) -> dict[str, Any]:
        assert_persistable(params)
        schedule_id = str(uuid.uuid4())
        interval_s = max(30, min(int(interval_s), 86_400))
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO schedules
                (id, kind, params_json, interval_s, enabled, next_at, created_at, requested_by)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (schedule_id, kind, _json(params), interval_s, now + interval_s, _iso(now), requested_by),
            )
        return self.get_schedule(schedule_id, reveal_params=False)  # type: ignore[return-value]

    def get_schedule(self, schedule_id: str, *, reveal_params: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._schedule(row, reveal_params=reveal_params) if row else None

    def list_schedules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
        return [self._schedule(row) for row in rows]

    def remove_schedule(self, schedule_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount == 1

    def due_schedules(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_at<=? ORDER BY next_at", (now,)
            ).fetchall()
        return [self._schedule(row, reveal_params=True) for row in rows]

    def advance_schedule(self, schedule_id: str, *, ran: bool) -> None:
        """Advance next_at. Default: wall-clock now + interval (drops missed
        ticks). With ZYVOR_SCHEDULE_CATCHUP=true, preserve cadence from the
        previous next_at so overdue schedules catch up one interval at a time.
        """
        catch_up = os.environ.get("ZYVOR_SCHEDULE_CATCHUP", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        with self.connect() as conn:
            row = conn.execute(
                "SELECT interval_s, next_at FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
            if row is None:
                return
            interval = float(row["interval_s"])
            now = time.time()
            if catch_up:
                base = float(row["next_at"] or now)
                nxt = base + interval
                # Leave nxt in the past when still behind so due_schedules
                # re-fires on the next loop until cadence catches up.
            else:
                nxt = now + interval
            if ran:
                conn.execute(
                    """UPDATE schedules SET next_at=?, runs=runs+1, last_at=? WHERE id=?""",
                    (nxt, _iso(), schedule_id),
                )
            else:
                conn.execute(
                    "UPDATE schedules SET next_at=? WHERE id=?",
                    (nxt, schedule_id),
                )

    def _schedule(self, row: sqlite3.Row, *, reveal_params: bool = False) -> dict[str, Any]:
        params = _loads(row["params_json"], {})
        return {
            "id": row["id"],
            "kind": row["kind"],
            "params": params if reveal_params else redact(params),
            "interval_s": row["interval_s"],
            "enabled": bool(row["enabled"]),
            "due_in_s": max(0, round(row["next_at"] - time.time())),
            "created": row["created_at"],
            "last_at": row["last_at"],
            "runs": row["runs"],
            "requested_by": row["requested_by"],
        }

    # Findings -------------------------------------------------------------
    def add_finding(
        self,
        source: str,
        severity: str,
        title: str,
        detail: str = "",
        url: str = "",
        location: str = "",
        fingerprint: str | None = None,
        category: str = "",
    ) -> int:
        now = _iso()
        title = str(redact(title))
        detail = str(redact(detail))
        url = str(redact(url))
        location = str(redact(location))
        category = category[:100]
        with self.connect() as conn:
            if fingerprint:
                existing = conn.execute("SELECT id FROM findings WHERE fingerprint=?", (fingerprint,)).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE findings SET severity=?, title=?, detail=?, url=?, location=?, category=?,
                        last_seen=?, occurrences=occurrences+1, status='open' WHERE id=?""",
                        (severity, title[:200], detail[:2000], url[:1000], location[:500], category, now, existing["id"]),
                    )
                    return int(existing["id"])
            cur = conn.execute(
                """INSERT INTO findings
                (fingerprint, source, severity, title, detail, url, location, category, status, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (fingerprint, source, severity, title[:200], detail[:2000], url[:1000], location[:500], category, now, now),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_findings(self, limit: int = 200, severity: str | None = None) -> dict[str, Any]:
        where = "WHERE status='open'"
        args: list[Any] = []
        if severity:
            where += " AND severity=?"
            args.append(severity)
        with self.connect() as conn:
            # `where` is always one of two fixed literals above, never user-controlled;
            # the actual values (severity, limit) are bound via `?` placeholders.
            total = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM findings {where}", args).fetchone()["n"]  # nosec B608
            )
            rows = conn.execute(
                f"SELECT * FROM findings {where} ORDER BY last_seen DESC LIMIT ?",  # nosec B608
                [*args, min(limit, 500)],
            ).fetchall()
            counts_rows = conn.execute(
                "SELECT severity, COUNT(*) AS n FROM findings WHERE status='open' GROUP BY severity"
            ).fetchall()
        counts = {row["severity"]: row["n"] for row in counts_rows}
        items = [dict(row) for row in rows]
        return {"findings": items, "total": total, "counts": counts}

    def clear_findings(self) -> int:
        with self.connect() as conn:
            return conn.execute("UPDATE findings SET status='closed' WHERE status='open'").rowcount

    # Engagements ------------------------------------------------------------
    def create_engagement(
        self,
        target_pattern: str,
        scope_statement: str,
        tier: str,
        *,
        authorized_by: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        engagement_id = str(uuid.uuid4())
        now = _iso()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO engagements
                (id, target_pattern, scope_statement, tier, authorized_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    engagement_id,
                    target_pattern[:500],
                    str(redact(scope_statement))[:2000],
                    tier,
                    authorized_by,
                    now,
                    expires_at,
                ),
            )
        return self.get_engagement(engagement_id)  # type: ignore[return-value]

    def get_engagement(self, engagement_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM engagements WHERE id=?", (engagement_id,)).fetchone()
        return dict(row) if row else None

    def list_engagements(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM engagements ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def revoke_engagement(self, engagement_id: str, *, revoked_by: str) -> bool:
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE engagements SET revoked_at=?, revoked_by=? WHERE id=? AND revoked_at IS NULL",
                (_iso(), revoked_by, engagement_id),
            ).rowcount
        return changed == 1

    # Audit ----------------------------------------------------------------
    def audit(
        self,
        action: str,
        *,
        actor: str = "",
        resource_type: str = "",
        resource_id: str = "",
        outcome: str = "success",
        detail: Any = None,
        client_ip: str = "",
        request_id: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO audit_events
                (at, actor, action, resource_type, resource_id, outcome, detail_json, client_ip, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _iso(), actor, action, resource_type, resource_id, outcome,
                    _json(redact(detail)) if detail is not None else None,
                    client_ip, request_id,
                ),
            )

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (min(limit, 1000),)
            ).fetchall()
        return [
            {
                **dict(row),
                "detail": _loads(row["detail_json"], None),
            }
            for row in rows
        ]

    # Webhook delivery replay protection ----------------------------------
    def record_webhook_delivery(
        self, delivery_id: str, event: str, payload_sha256: str, *, ttl_s: int = 7 * 86400
    ) -> bool:
        """Return False for duplicate delivery IDs."""
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        now = time.time()
        with self.connect() as conn:
            conn.execute("DELETE FROM webhook_deliveries WHERE received_at < ?", (now - ttl_s,))
            try:
                conn.execute(
                    "INSERT INTO webhook_deliveries(delivery_id,event,received_at,payload_sha256) VALUES(?,?,?,?)",
                    (delivery_id, event, now, payload_sha256),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    # Requirements -----------------------------------------------------------
    def upsert_requirement(
        self,
        requirement_id: str,
        *,
        source_type: str,
        origin_id: str | None,
        title: str,
        content: dict[str, Any],
        quality_score: float | None = None,
        quality_issues: list[dict[str, Any]] | None = None,
        data_models: list[str] | None = None,
        flows: list[str] | None = None,
        model_dependencies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Insert a new version only if `content` differs from the current
        latest — this is the change-detection primitive impact analysis is
        built on. Returns the persisted requirement plus whether this call
        actually created a new version (vs. re-confirming an unchanged one)."""
        assert_persistable(content)
        content_json = _json(content)
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        now = _iso()
        title = str(redact(title))[:300]
        with self.connect() as conn:
            # BEGIN IMMEDIATE (matching claim_job()'s pattern below) — this
            # is a read-then-conditionally-write on `requirements.id`'s own
            # PRIMARY KEY, so without an explicit write lock two concurrent
            # upserts for the same id (a schedule and a webhook-triggered
            # run overlapping, say) can both read "does not exist yet" and
            # both attempt the INSERT, raising an unhandled IntegrityError.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT latest_version FROM requirements WHERE id=?", (requirement_id,)
            ).fetchone()
            if existing is None:
                version = 1
                is_new_version = True
                conn.execute(
                    """INSERT INTO requirements
                    (id, source_type, origin_id, title, latest_version, quality_score, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (requirement_id, source_type, origin_id, title, version, quality_score, now, now),
                )
            else:
                current_version = conn.execute(
                    "SELECT content_hash FROM requirement_versions WHERE requirement_id=? AND version=?",
                    (requirement_id, existing["latest_version"]),
                ).fetchone()
                is_new_version = current_version is None or current_version["content_hash"] != content_hash
                version = existing["latest_version"] + 1 if is_new_version else existing["latest_version"]
                if is_new_version:
                    conn.execute(
                        """UPDATE requirements
                        SET source_type=?, origin_id=?, title=?, latest_version=?, quality_score=?, updated_at=?
                        WHERE id=?""",
                        (source_type, origin_id, title, version, quality_score, now, requirement_id),
                    )
                else:
                    conn.execute(
                        "UPDATE requirements SET quality_score=?, updated_at=? WHERE id=?",
                        (quality_score, now, requirement_id),
                    )
            if is_new_version:
                conn.execute(
                    """INSERT INTO requirement_versions
                    (requirement_id, version, content_json, content_hash, quality_score, quality_issues_json,
                     created_at, data_models_json, flows_json, model_dependencies_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        requirement_id,
                        version,
                        content_json,
                        content_hash,
                        quality_score,
                        _json(quality_issues) if quality_issues is not None else None,
                        now,
                        _json(data_models) if data_models is not None else None,
                        _json(flows) if flows is not None else None,
                        _json(model_dependencies) if model_dependencies is not None else None,
                    ),
                )
            row = conn.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,)).fetchone()
            conn.execute("COMMIT")
        result = dict(row)
        result["is_new_version"] = is_new_version
        result["previous_version"] = version - 1 if is_new_version and version > 1 else None
        return result

    def get_requirement(self, requirement_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,)).fetchone()
            if row is None:
                return None
            version = conn.execute(
                "SELECT * FROM requirement_versions WHERE requirement_id=? AND version=?",
                (requirement_id, row["latest_version"]),
            ).fetchone()
        result = dict(row)
        result["content"] = _loads(version["content_json"], None) if version else None
        result["quality_issues"] = _loads(version["quality_issues_json"], []) if version else []
        result["data_models"] = _loads(version["data_models_json"], []) if version else []
        result["flows"] = _loads(version["flows_json"], []) if version else []
        result["model_dependencies"] = (
            _loads(version["model_dependencies_json"], []) if version else []
        )
        return result

    def list_requirements(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requirements ORDER BY updated_at DESC LIMIT ?", (min(limit, 500),)
            ).fetchall()
        return [dict(row) for row in rows]

    def requirement_history(self, requirement_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requirement_versions WHERE requirement_id=? ORDER BY version",
                (requirement_id,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["content"] = _loads(item.pop("content_json"), None)
            item["quality_issues"] = _loads(item.pop("quality_issues_json"), [])
            item["data_models"] = _loads(item.pop("data_models_json"), [])
            item["flows"] = _loads(item.pop("flows_json"), [])
            item["model_dependencies"] = _loads(item.pop("model_dependencies_json"), [])
            history.append(item)
        return history

    def link_requirement_test(self, requirement_id: str, test_path: str) -> None:
        """Records that `test_path` was generated from the requirement's
        *current* latest version — called right after generation, so
        "latest" is always the version that was just generated from."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT latest_version FROM requirements WHERE id=?", (requirement_id,)
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """INSERT OR IGNORE INTO requirement_test_links
                (requirement_id, requirement_version, test_path, created_at)
                VALUES (?, ?, ?, ?)""",
                (requirement_id, row["latest_version"], test_path, _iso()),
            )

    def linked_tests(self, requirement_id: str, version: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT test_path FROM requirement_test_links
                WHERE requirement_id=? AND requirement_version=? ORDER BY test_path""",
                (requirement_id, version),
            ).fetchall()
        return [row["test_path"] for row in rows]

    def requirement_impact_graph(self) -> dict[str, Any]:
        """Groups every requirement's *latest* version by data models and flows,
        and derives undirected `model_edges` from co-occurrence (two models on
        the same requirement → an edge). Bounded impact analysis — not a full
        typed dependency graph (see ROADMAP.md)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.id, rv.version, rv.data_models_json, rv.flows_json,
                          rv.model_dependencies_json
                FROM requirements r
                JOIN requirement_versions rv
                  ON rv.requirement_id = r.id AND rv.version = r.latest_version"""
            ).fetchall()
        data_models: dict[str, list[str]] = {}
        flows: dict[str, dict[str, Any]] = {}
        edge_counts: dict[tuple[str, str], int] = {}
        typed: list[dict[str, Any]] = []
        for row in rows:
            req_id = row["id"]
            models = [str(m) for m in _loads(row["data_models_json"], []) if str(m).strip()]
            for model in models:
                data_models.setdefault(model, []).append(req_id)
            for i, a in enumerate(models):
                for b in models[i + 1 :]:
                    edge = (a, b) if a <= b else (b, a)
                    edge_counts[edge] = edge_counts.get(edge, 0) + 1
            for dep in _loads(row["model_dependencies_json"], []):
                if not isinstance(dep, dict):
                    continue
                src = str(dep.get("source") or "").strip()
                tgt = str(dep.get("target") or "").strip()
                if not src or not tgt:
                    continue
                typed.append(
                    {
                        "source": src,
                        "target": tgt,
                        "relation": str(dep.get("relation") or "depends_on"),
                        "requirement_id": req_id,
                    }
                )
            for flow in _loads(row["flows_json"], []):
                bucket = flows.setdefault(flow, {"requirements": [], "tests": []})
                bucket["requirements"].append(req_id)
                bucket["tests"].extend(self.linked_tests(req_id, row["version"]))
        for bucket in flows.values():
            bucket["tests"] = sorted(set(bucket["tests"]))
        model_edges = [
            {"a": a, "b": b, "weight": w, "via_requirements": w}
            for (a, b), w in sorted(edge_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {
            "data_models": data_models,
            "flows": flows,
            "model_edges": model_edges,
            "model_dependencies": typed,
        }


_default_store: Any | None = None
_default_lock = threading.Lock()


def get_store() -> Any:
    """Returns MissionControlStore (SQLite, the default) or PostgresStore --
    selected by ZYVOR_STATE_DB's scheme. Both implement the identical public
    method surface (see postgres_store.py's module docstring), so every
    caller elsewhere in this codebase is unaffected either way.

    The PostgresStore import is deliberately local, not module-level: `psycopg`
    is an optional extra (`pip install "zyvor-argus[postgres]"`), and this
    module is imported by nearly every part of the app -- a top-level import
    here would make psycopg a hard dependency for the SQLite-only default.
    """
    global _default_store
    with _default_lock:
        if _default_store is None:
            configured = os.environ.get("ZYVOR_STATE_DB", "")
            if configured.startswith("postgresql://") or configured.startswith("postgres://"):
                from orchestrator.persistence.postgres_store import PostgresStore

                _default_store = PostgresStore(configured)
            else:
                _default_store = MissionControlStore()
        return _default_store