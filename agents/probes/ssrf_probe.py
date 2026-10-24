# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Probe whether the *target* appears to fetch attacker-controlled URLs (SSRF).

Sends canary values into URL-like query params and looks for response
differences / error leaks that suggest server-side fetch. Does not open
listener callbacks; uses only well-known internal addresses as payloads.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlparse

from agents.probes._scan_common import Log, client, emit, with_query

_URL_PARAM_HINTS = (
    "url", "uri", "path", "dest", "destination", "redirect", "redirect_uri",
    "next", "return", "return_to", "callback", "webhook", "feed", "src", "source",
    "link", "target", "fetch", "proxy", "u",
)

_CANARIES = (
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
)

_SSRF_HINTS = (
    "connection refused",
    "connection timed out",
    "failed to connect",
    "name or service not known",
    "nodename nor servname",
    "metadata",
    "ami-id",
    "instance-id",
)


def run_ssrf_probe(
    url: str,
    *,
    insecure: bool = False,
    param: str | None = None,
    log: Log = None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    existing = [k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    candidates = []
    if param:
        candidates = [param]
    else:
        candidates = [k for k in existing if k.lower() in _URL_PARAM_HINTS] or list(_URL_PARAM_HINTS[:6])
    candidates = candidates[:8]

    emit(log, f"ssrf_probe: {url} params={candidates}")
    findings: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []

    with client(insecure=insecure, timeout=10) as c:
        try:
            baseline = c.get(url, follow_redirects=True)
            base_len = len(baseline.content)
            base_status = baseline.status_code
            base_body = baseline.text[:30_000].lower()
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "error": str(exc)[:200], "findings": [], "probes": []}

        for p in candidates:
            for canary in _CANARIES:
                target = with_query(url, {p: canary})
                try:
                    r = c.get(target, follow_redirects=True)
                    body = r.text[:30_000]
                    body_l = body.lower()
                    delta = abs(len(r.content) - base_len)
                    hint_hit = any(h in body_l and h not in base_body for h in _SSRF_HINTS)
                    status_shift = r.status_code != base_status and r.status_code not in (400, 404)
                    row = {
                        "param": p,
                        "canary": canary,
                        "status": r.status_code,
                        "bytes": len(r.content),
                        "hint_hit": hint_hit,
                        "delta_bytes": delta,
                    }
                    probes.append(row)
                    if hint_hit or (status_shift and delta > 200):
                        findings.append({
                            "severity": "high",
                            "category": "ssrf",
                            "title": f"possible SSRF via ?{p}=",
                            "detail": f"canary={canary} status={r.status_code} delta={delta} hint={hint_hit}",
                            "where": f"?{p}",
                        })
                        break
                except Exception as exc:  # noqa: BLE001
                    probes.append({"param": p, "canary": canary, "error": str(exc)[:120]})

    # Dedupe
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in findings:
        if item["where"] not in seen:
            seen.add(item["where"])
            unique.append(item)

    emit(log, f"ssrf_probe: {len(unique)} finding(s)")
    return {"url": url, "params_tested": candidates, "probes": probes, "findings": unique, "issues": [f["title"] for f in unique]}
