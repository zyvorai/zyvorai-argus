# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Failure studio — one payload per job that stitches cases, artifacts,
classification, and a quarantine recommendation. Does not fetch remote
URLs; it only reads the job record and local report paths already on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.intelligence.classify import classify_error, classify_history
from orchestrator.intelligence.quarantine import is_quarantined, test_key


def _artifact_exists(href: str | None, repo: Path) -> bool:
    if not href:
        return False
    raw = href.split("?", 1)[0]
    if raw.startswith("/reports/"):
        candidate = repo / raw.lstrip("/")
    elif raw.startswith("reports/"):
        candidate = repo / raw
    else:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo / candidate
    try:
        return candidate.is_file()
    except OSError:
        return False


def build_studio(job: dict[str, Any], *, repo: Path | None = None) -> dict[str, Any]:
    from orchestrator.paths import repo_root

    root = repo if repo is not None else repo_root()
    raw_result = job.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    raw_cases = result.get("cases")
    cases: list[Any] = raw_cases if isinstance(raw_cases, list) else []
    rows: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "")
        file = str(raw.get("file") or raw.get("spec") or "")
        error = str(raw.get("error") or "")
        status = str(raw.get("status") or "unknown")
        category = classify_error(error) if status != "passed" else "healthy"
        history = classify_history(
            [status],
            last_error=error,
        )
        q = is_quarantined(title, file, root=root)
        rows.append(
            {
                "title": title,
                "file": file,
                "key": test_key(title, file),
                "status": status,
                "duration_ms": raw.get("duration_ms"),
                "error": error[:1200],
                "hint": raw.get("hint") or "",
                "category": category,
                "recommend_quarantine": history["recommend_quarantine"],
                "quarantined": bool(q),
                "quarantine": q,
                "video": raw.get("video"),
                "trace": raw.get("trace"),
                "video_present": _artifact_exists(raw.get("video"), root),
                "trace_present": _artifact_exists(raw.get("trace"), root),
                "console_logs": raw.get("console_logs") or [],
                "network_errors": raw.get("network_errors") or [],
            }
        )

    failed = [r for r in rows if r["status"] != "passed"]
    return {
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "finished_at": job.get("finished_at"),
        "failed_count": len(failed),
        "total_count": len(rows),
        "cases": rows,
        "primary": failed[0] if failed else None,
    }
