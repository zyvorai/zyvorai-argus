# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Pure helpers for db_migration_check: parse the sandbox runner's
'CHECK: {json}' lines and compare a post-migration run against the
pre-migration baseline. No database access here -- the runner (see
runner_script.py) does the querying inside the sandbox; this only works on
the row counts, column names and result digests it prints."""

from __future__ import annotations

import json
from typing import Any


def parse_check_lines(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("CHECK:"):
            continue
        try:
            entry = json.loads(line[len("CHECK:"):].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("name"):
            out.append(entry)
    return out


def compare_to_baseline(
    checks: list[dict[str, Any]], baseline: list[dict[str, Any]], stable: set[str]
) -> list[dict[str, str]]:
    """Return one drift record per difference between the post-migration
    results (`checks`) and the pre-migration `baseline`.

    - A baseline check that is missing after the migration is always drift.
    - Columns changing is drift for every check (a migration that renames or
      drops a column an invariant reads should be noticed).
    - A changed row count / digest is drift only for checks named in
      `stable`; other checks are expected to change (e.g. a backfill).
    """
    now = {c["name"]: c for c in checks}
    drift: list[dict[str, str]] = []
    for before in baseline:
        name = before["name"]
        after = now.get(name)
        if after is None:
            drift.append({"check": name, "kind": "missing", "detail": "check did not run after the migration"})
            continue
        if before.get("columns") != after.get("columns"):
            drift.append({
                "check": name, "kind": "columns",
                "detail": f"columns changed from {before.get('columns')} to {after.get('columns')}",
            })
        if name in stable and before.get("digest") != after.get("digest"):
            drift.append({
                "check": name, "kind": "data",
                "detail": f"result changed (rows {before.get('rows')} -> {after.get('rows')}) but the check is marked stable",
            })
    return drift
