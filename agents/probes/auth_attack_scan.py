# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Auth attack *hygiene* battery — JWT alg=none, weak cookie flags, basic enum hints.

Explicitly does **not** brute-force or credential-stuff. Non-destructive checks
only against responses/cookies from the target URL.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from agents.probes._scan_common import Log, client, emit

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _jwt_header(token: str) -> dict[str, Any] | None:
    try:
        header = json.loads(_b64url_decode(token.split(".")[0]))
        return header if isinstance(header, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _forge_alg_none(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    header = {"alg": "none", "typ": "JWT"}
    raw = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{parts[1]}."


def run_auth_attack_scan(
    url: str,
    *,
    insecure: bool = False,
    login_url: str | None = None,
    log: Log = None,
) -> dict[str, Any]:
    emit(log, f"auth_attack_scan: {url}")
    findings: list[dict[str, Any]] = []
    tokens_seen: list[str] = []

    with client(insecure=insecure) as c:
        try:
            r = c.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "error": str(exc)[:200], "findings": []}

        body = r.text[:100_000]
        tokens_seen = list(dict.fromkeys(_JWT_RE.findall(body) + _JWT_RE.findall(r.headers.get("authorization", ""))))[:5]

        # Cookie flags on Set-Cookie
        set_cookie = r.headers.get("set-cookie") or ""
        if set_cookie:
            lower = set_cookie.lower()
            if "httponly" not in lower:
                findings.append({
                    "severity": "medium",
                    "category": "auth-cookie",
                    "title": "session cookie missing HttpOnly",
                    "detail": set_cookie[:160],
                    "where": "Set-Cookie",
                })
            if url.startswith("https://") and "secure" not in lower:
                findings.append({
                    "severity": "medium",
                    "category": "auth-cookie",
                    "title": "session cookie missing Secure on HTTPS",
                    "detail": set_cookie[:160],
                    "where": "Set-Cookie",
                })

        for token in tokens_seen:
            header = _jwt_header(token)
            if not header:
                continue
            alg = str(header.get("alg") or "")
            if alg.lower() == "none":
                findings.append({
                    "severity": "critical",
                    "category": "jwt-alg-none",
                    "title": "JWT already uses alg=none",
                    "detail": f"header={header}",
                    "where": "jwt",
                })
            forged = _forge_alg_none(token)
            if forged and login_url:
                try:
                    probe = c.get(
                        login_url,
                        headers={"Authorization": f"Bearer {forged}"},
                        follow_redirects=True,
                    )
                    if probe.status_code < 400:
                        findings.append({
                            "severity": "critical",
                            "category": "jwt-alg-none",
                            "title": "endpoint accepted alg=none JWT",
                            "detail": f"status={probe.status_code} on {login_url}",
                            "where": login_url,
                        })
                except Exception:  # noqa: BLE001
                    pass

        # Username-enumeration hint: different responses for likely-valid vs garbage user
        target_login = login_url or url
        try:
            a = c.post(target_login, data={"username": "admin", "password": "DefinitelyWrongPass1!"}, follow_redirects=True)
            b = c.post(target_login, data={"username": "argus_no_such_user_zz", "password": "DefinitelyWrongPass1!"}, follow_redirects=True)
            if a.status_code == b.status_code and abs(len(a.content) - len(b.content)) > 40:
                findings.append({
                    "severity": "low",
                    "category": "user-enumeration",
                    "title": "possible username enumeration via login response length",
                    "detail": f"admin_len={len(a.content)} missing_len={len(b.content)}",
                    "where": target_login,
                })
        except Exception:  # noqa: BLE001
            pass

    emit(log, f"auth_attack_scan: {len(findings)} finding(s), tokens={len(tokens_seen)}")
    return {
        "url": url,
        "jwt_tokens_seen": len(tokens_seen),
        "findings": findings,
        "issues": [f["title"] for f in findings],
    }
