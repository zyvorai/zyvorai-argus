# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Bounded IDOR / horizontal access probe — adjacent numeric IDs only.

Compares response status/size for id±N. Optional Cookie / Authorization
header lets the scan run as an authenticated user. No mass ID brute force.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from agents.probes._scan_common import Log, client, emit

_ID_PARAM_HINTS = ("id", "user_id", "userid", "uid", "account_id", "order_id", "doc_id", "object_id")
_PATH_ID_RE = re.compile(r"/(\d{1,12})(/|$)")


def _swap_query_id(url: str, param: str, new_id: int) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[param] = str(new_id)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


def _swap_path_id(url: str, new_id: int) -> str | None:
    p = urlparse(url)
    new_path, n = _PATH_ID_RE.subn(rf"/{new_id}\2", p.path, count=1)
    if n == 0:
        return None
    return urlunparse((p.scheme, p.netloc, new_path, p.params, p.query, p.fragment))


def run_idor_scan(
    url: str,
    *,
    insecure: bool = False,
    cookie: str = "",
    authorization: str = "",
    delta: int = 1,
    log: Log = None,
) -> dict[str, Any]:
    delta = max(1, min(int(delta), 5))
    emit(log, f"idor_scan: {url} delta=±{delta}")
    headers: dict[str, str] = {}
    if cookie:
        headers["Cookie"] = cookie[:4000]
    if authorization:
        headers["Authorization"] = authorization[:2000]

    findings: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []

    with client(insecure=insecure) as c:
        try:
            baseline = c.get(url, headers=headers, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "error": str(exc)[:200], "findings": [], "probes": []}

        base_status = baseline.status_code
        base_len = len(baseline.content)
        if base_status >= 400:
            return {
                "url": url,
                "error": f"baseline returned {base_status} — provide an authorized object URL",
                "findings": [],
                "probes": [],
            }

        parsed = urlparse(url)
        q = dict(parse_qsl(parsed.query, keep_blank_values=True))
        id_params = [k for k in q if k.lower() in _ID_PARAM_HINTS and str(q[k]).isdigit()]
        path_match = _PATH_ID_RE.search(parsed.path)

        candidates: list[tuple[str, str, int]] = []  # (kind, key, base_id)
        for k in id_params:
            candidates.append(("query", k, int(q[k])))
        if path_match:
            candidates.append(("path", "path", int(path_match.group(1))))

        if not candidates:
            return {
                "url": url,
                "error": "no numeric id in query/path to mutate",
                "findings": [],
                "probes": [],
            }

        for kind, key, base_id in candidates:
            for offset in range(-delta, delta + 1):
                if offset == 0:
                    continue
                new_id = base_id + offset
                if new_id < 0:
                    continue
                if kind == "query":
                    target = _swap_query_id(url, key, new_id)
                    where = f"?{key}={new_id}"
                else:
                    swapped = _swap_path_id(url, new_id)
                    if not swapped:
                        continue
                    target = swapped
                    where = f"path:{new_id}"
                try:
                    r = c.get(target, headers=headers, follow_redirects=True)
                    probes.append({
                        "where": where,
                        "status": r.status_code,
                        "bytes": len(r.content),
                    })
                    # Same status + similar body size as authorized object → possible IDOR
                    if r.status_code == base_status and abs(len(r.content) - base_len) < max(80, int(base_len * 0.15)):
                        findings.append({
                            "severity": "high",
                            "category": "idor",
                            "title": f"possible IDOR — adjacent id {new_id} returned similar object",
                            "detail": f"baseline={base_id} status={r.status_code} bytes={len(r.content)} vs {base_len}",
                            "where": where,
                        })
                except Exception as exc:  # noqa: BLE001
                    probes.append({"where": where, "error": str(exc)[:120]})

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in findings:
        if item["where"] not in seen:
            seen.add(item["where"])
            unique.append(item)

    emit(log, f"idor_scan: {len(unique)} finding(s)")
    return {"url": url, "probes": probes, "findings": unique, "issues": [f["title"] for f in unique]}
