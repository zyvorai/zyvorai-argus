# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Target-app CSRF posture probe — forms, tokens, cookie SameSite.

Detection-oriented: finds state-changing forms missing anti-CSRF tokens and
cookies without SameSite. Does not forge authenticated cross-site requests
against third parties.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from agents.probes._scan_common import Log, client, emit

_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input\b([^>]*)/?>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_CSRF_NAME_HINTS = re.compile(r"csrf|xsrf|_token|authenticity_token|__requestverificationtoken", re.I)


def _attrs(blob: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(blob)}


def run_csrf_probe(url: str, *, insecure: bool = False, log: Log = None) -> dict[str, Any]:
    emit(log, f"csrf_probe: {url}")
    findings: list[dict[str, Any]] = []
    forms_out: list[dict[str, Any]] = []

    with client(insecure=insecure) as c:
        try:
            r = c.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "error": str(exc)[:200], "findings": [], "forms": []}

        body = r.text[:200_000]
        set_cookie = r.headers.get("set-cookie") or ""
        if set_cookie and "samesite" not in set_cookie.lower():
            findings.append({
                "severity": "medium",
                "category": "csrf-cookie",
                "title": "Set-Cookie missing SameSite",
                "detail": set_cookie[:160],
                "where": "Set-Cookie",
            })

        for form_m in _FORM_RE.finditer(body):
            form_attrs = _attrs(form_m.group(1))
            method = (form_attrs.get("method") or "get").upper()
            action = urljoin(str(r.url), form_attrs.get("action") or "")
            inputs = [_attrs(m.group(1)) for m in _INPUT_RE.finditer(form_m.group(2))]
            names = [i.get("name", "") for i in inputs if i.get("name")]
            has_token = any(_CSRF_NAME_HINTS.search(n or "") for n in names)
            forms_out.append({
                "method": method,
                "action": action[:300],
                "inputs": len(names),
                "has_csrf_token": has_token,
            })
            if method in ("POST", "PUT", "PATCH", "DELETE") and not has_token:
                findings.append({
                    "severity": "high",
                    "category": "csrf-form",
                    "title": f"state-changing form without CSRF token ({method})",
                    "detail": f"action={action[:200]} inputs={names[:8]}",
                    "where": action[:200],
                })

    emit(log, f"csrf_probe: {len(forms_out)} form(s), {len(findings)} finding(s)")
    return {
        "url": url,
        "forms": forms_out,
        "findings": findings,
        "issues": [f["title"] for f in findings],
    }
