# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Engagement-gated DAST aggregator — built-in templates (+ optional nuclei).

Runs a bounded in-process template set (injection + CSRF + open-redirect +
security-header quick checks). If ``ZYVOR_DAST_NUCLEI_BIN`` points at a
``nuclei`` binary, also runs a capped nuclei pass and merges findings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from agents.probes._scan_common import Log, client, emit, with_query
from agents.probes.csrf_probe import run_csrf_probe
from agents.probes.injection_scan import run_injection_scan


def _open_redirect_check(url: str, *, insecure: bool, log: Log) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    parsed = urlparse(url)
    params = [k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    redirect_params = [p for p in params if p.lower() in {
        "next", "url", "redirect", "redirect_uri", "return", "return_to", "continue", "dest", "destination",
    }] or ["next", "redirect", "url"]
    canary = "https://example.com/argus-open-redirect"
    canary_host = "example.com"

    def _redirects_to_canary(loc: str) -> bool:
        if canary in loc:
            return True
        # Protocol-relative or absolute Location → compare hostname, not prefix.
        candidate = loc
        if loc.startswith("//"):
            candidate = "https:" + loc
        elif "://" not in loc:
            return False
        host = (urlparse(candidate).hostname or "").lower()
        return host == canary_host

    with client(insecure=insecure) as c:
        for param in redirect_params[:4]:
            target = with_query(url, {param: canary})
            try:
                r = c.get(target, follow_redirects=False)
                loc = r.headers.get("location") or ""
                if _redirects_to_canary(loc):
                    findings.append({
                        "severity": "high",
                        "category": "open-redirect",
                        "title": f"open redirect via ?{param}=",
                        "detail": f"Location: {loc[:200]}",
                        "where": f"?{param}",
                    })
            except Exception:  # noqa: BLE001
                continue
    emit(log, f"dast open-redirect: {len(findings)} hit(s)")
    return findings


def _header_quick_check(url: str, *, insecure: bool, log: Log) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    with client(insecure=insecure) as c:
        try:
            r = c.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return [{"severity": "low", "category": "dast-error", "title": "header check failed",
                     "detail": str(exc)[:160], "where": url}]
        headers = {k.lower(): v for k, v in r.headers.items()}
        scheme = urlparse(str(r.url)).scheme
        for name, sev in (
            ("content-security-policy", "medium"),
            ("strict-transport-security", "medium"),
            ("x-content-type-options", "low"),
            ("x-frame-options", "low"),
        ):
            if name in headers:
                continue
            if name == "strict-transport-security" and scheme != "https":
                continue
            findings.append({
                "severity": sev,
                "category": "missing-security-header",
                "title": f"missing {name}",
                "detail": "not present on primary response",
                "where": "headers",
            })
    emit(log, f"dast headers: {len(findings)} issue(s)")
    return findings


def _run_nuclei(url: str, *, timeout_s: int, log: Log) -> list[dict[str, Any]]:
    bin_path = (os.environ.get("ZYVOR_DAST_NUCLEI_BIN") or "").strip() or shutil.which("nuclei")
    if not bin_path:
        emit(log, "dast_scan: nuclei not configured — skipping external templates")
        return []
    emit(log, f"dast_scan: running nuclei via {bin_path}")
    try:
        proc = subprocess.run(
            [
                bin_path, "-u", url, "-jsonl", "-silent",
                "-c", "10", "-rl", "50", "-timeout", "5",
                "-severity", "critical,high,medium",
            ],
            capture_output=True,
            text=True,
            timeout=max(30, min(timeout_s, 300)),
            check=False,
        )
    except FileNotFoundError:
        emit(log, "dast_scan: nuclei binary not found")
        return []
    except subprocess.TimeoutExpired:
        emit(log, "dast_scan: nuclei timed out")
        return []

    findings: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines()[:200]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = row.get("info") or {}
        sev = str(info.get("severity") or "medium").lower()
        if sev not in {"critical", "high", "medium", "low"}:
            sev = "medium"
        findings.append({
            "severity": sev,
            "category": "nuclei",
            "title": str(info.get("name") or row.get("template-id") or "nuclei finding")[:200],
            "detail": str(info.get("description") or row.get("matched-at") or "")[:400],
            "where": str(row.get("matched-at") or url)[:200],
        })
    emit(log, f"dast_scan: nuclei reported {len(findings)} finding(s)")
    return findings


def run_dast_scan(
    url: str,
    *,
    insecure: bool = False,
    max_requests: int = 40,
    timeout_s: int = 120,
    modules: list[str] | None = None,
    log: Log = None,
) -> dict[str, Any]:
    """Run selected DAST modules and return aggregated findings."""
    selected = modules or ["headers", "injection", "csrf", "open_redirect", "nuclei"]
    selected = [m for m in selected if m in {"headers", "injection", "csrf", "open_redirect", "nuclei"}]
    emit(log, f"dast_scan: {url} modules={selected}")

    # Normalize to origin+path (keep query — injection needs it)
    parsed = urlparse(url)
    if not parsed.scheme:
        url = urlunparse(("https", parsed.path, "", "", "", ""))

    all_findings: list[dict[str, Any]] = []
    module_results: dict[str, Any] = {}

    if "headers" in selected:
        hits = _header_quick_check(url, insecure=insecure, log=log)
        module_results["headers"] = {"count": len(hits)}
        all_findings.extend(hits)
    if "injection" in selected:
        inj = run_injection_scan(url, insecure=insecure, max_requests=max_requests, log=log)
        module_results["injection"] = {"requests": inj.get("requests"), "count": len(inj.get("findings") or [])}
        all_findings.extend(inj.get("findings") or [])
    if "csrf" in selected:
        csrf = run_csrf_probe(url, insecure=insecure, log=log)
        module_results["csrf"] = {"forms": len(csrf.get("forms") or []), "count": len(csrf.get("findings") or [])}
        all_findings.extend(csrf.get("findings") or [])
    if "open_redirect" in selected:
        hits = _open_redirect_check(url, insecure=insecure, log=log)
        module_results["open_redirect"] = {"count": len(hits)}
        all_findings.extend(hits)
    if "nuclei" in selected:
        hits = _run_nuclei(url, timeout_s=timeout_s, log=log)
        module_results["nuclei"] = {"count": len(hits)}
        all_findings.extend(hits)

    # Dedupe by title+where
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in all_findings:
        key = (item.get("title", ""), item.get("where", ""))
        if key not in seen:
            seen.add(key)
            unique.append(item)

    emit(log, f"dast_scan: done — {len(unique)} unique finding(s)")
    return {
        "url": url,
        "modules": selected,
        "module_results": module_results,
        "findings": unique,
        "issues": [f.get("title", "") for f in unique],
        "total": len(unique),
    }
