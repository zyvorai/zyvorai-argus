# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Systematic (bounded) SQLi / XSS / path-traversal probe battery.

Sends a small fixed payload set against common query params and flags
error-signature / reflection hits. Non-destructive — no time-based blind
sleep bombs, no UNION dumps. Requires ``exploit`` engagement + DAST opt-in.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlparse

from agents.probes._scan_common import Log, client, emit, with_query

_COMMON_PARAMS = ("id", "q", "search", "query", "name", "page", "user", "file", "path", "dir")

_SQLI_PAYLOADS = (
    "'",
    "\"",
    "1' OR '1'='1",
    "1\" OR \"1\"=\"1",
)
_SQLI_ERRORS = re.compile(
    r"(sql syntax|mysql_fetch|ORA-\d+|PostgreSQL.*ERROR|SQLite\.Exception|"
    r"Unclosed quotation mark|ODBC.*Driver|pg_query|SQLSTATE)",
    re.IGNORECASE,
)

_XSS_PAYLOAD = "<argusxss123>"
_XSS_PAYLOADS = (_XSS_PAYLOAD, f"\"'><script>{_XSS_PAYLOAD}</script>")

_TRAVERSAL_PAYLOADS = (
    "../../../../etc/passwd",
    "..%2f..%2f..%2f..%2fetc%2fpasswd",
)
_PASSWD_HINT = re.compile(r"root:.*:0:0:")


def _params_for(url: str) -> list[str]:
    existing = [k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    return list(dict.fromkeys([*existing, *_COMMON_PARAMS]))[:12]


def run_injection_scan(
    url: str,
    *,
    insecure: bool = False,
    max_requests: int = 40,
    log: Log = None,
) -> dict[str, Any]:
    params = _params_for(url)
    max_requests = max(5, min(int(max_requests), 80))
    emit(log, f"injection_scan: {url} params={params}")
    findings: list[dict[str, Any]] = []
    requests_made = 0

    with client(insecure=insecure) as c:
        try:
            baseline = c.get(url, follow_redirects=True)
            baseline_body = baseline.text[:50_000]
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "error": str(exc)[:200], "findings": [], "requests": 0}

        for param in params:
            if requests_made >= max_requests:
                break
            for payload in _SQLI_PAYLOADS:
                if requests_made >= max_requests:
                    break
                target = with_query(url, {param: payload})
                try:
                    r = c.get(target, follow_redirects=True)
                    requests_made += 1
                    body = r.text[:50_000]
                    if _SQLI_ERRORS.search(body) and not _SQLI_ERRORS.search(baseline_body):
                        findings.append({
                            "severity": "high",
                            "category": "sql-injection",
                            "title": f"possible SQLi via ?{param}=",
                            "detail": f"error signature after payload {payload!r}",
                            "where": f"?{param}",
                        })
                        break
                except Exception:  # noqa: BLE001
                    requests_made += 1

            for payload in _XSS_PAYLOADS:
                if requests_made >= max_requests:
                    break
                target = with_query(url, {param: payload})
                try:
                    r = c.get(target, follow_redirects=True)
                    requests_made += 1
                    if _XSS_PAYLOAD in r.text[:50_000]:
                        findings.append({
                            "severity": "high",
                            "category": "reflected-xss",
                            "title": f"reflected XSS via ?{param}=",
                            "detail": "probe marker reflected unescaped in response body",
                            "where": f"?{param}",
                        })
                        break
                except Exception:  # noqa: BLE001
                    requests_made += 1

            for payload in _TRAVERSAL_PAYLOADS:
                if requests_made >= max_requests:
                    break
                target = with_query(url, {param: payload})
                try:
                    r = c.get(target, follow_redirects=True)
                    requests_made += 1
                    if _PASSWD_HINT.search(r.text[:20_000]):
                        findings.append({
                            "severity": "critical",
                            "category": "path-traversal",
                            "title": f"path traversal via ?{param}=",
                            "detail": "/etc/passwd content pattern in response",
                            "where": f"?{param}",
                        })
                        break
                except Exception:  # noqa: BLE001
                    requests_made += 1

    # Dedupe by (category, where)
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in findings:
        key = (item["category"], item["where"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    emit(log, f"injection_scan: {len(unique)} finding(s) after {requests_made} request(s)")
    return {
        "url": url,
        "params_tested": params,
        "requests": requests_made,
        "findings": unique,
        "issues": [f["title"] for f in unique],
    }
