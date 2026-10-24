# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""TLS protocol / cipher-suite grading beyond basic cert metadata.

Connects with the stdlib ``ssl`` module, records negotiated protocol + cipher,
and flags weak suites (NULL/export/RC4/DES/MD5/anon). ``active_recon`` tier.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any
from urllib.parse import urlparse

from agents.probes._scan_common import Log, emit, hostname

_WEAK_TOKENS = ("NULL", "EXPORT", "RC4", "DES", "MD5", "ANON", "aNULL", "eNULL", "ADH", "AECDH")
_PROTOCOLS: tuple[tuple[str, ssl.TLSVersion | None], ...] = (
    ("TLSv1.3", getattr(ssl.TLSVersion, "TLSv1_3", None)),
    ("TLSv1.2", getattr(ssl.TLSVersion, "TLSv1_2", None)),
    ("TLSv1.1", getattr(ssl.TLSVersion, "TLSv1_1", None)),
    ("TLSv1.0", getattr(ssl.TLSVersion, "TLSv1", None)),
)


def _try_connect(host: str, port: int, *, min_v: ssl.TLSVersion | None, max_v: ssl.TLSVersion | None) -> dict[str, Any]:
    # Intentional: this probe enumerates whether the target still offers legacy
    # TLS (TLSv1/1.1). Restricting to TLSv1.2+ would defeat the check.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # codeql[py/insecure-protocol]
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if min_v is not None:
        ctx.minimum_version = min_v
    if max_v is not None:
        ctx.maximum_version = max_v
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cipher = tls.cipher()  # (name, protocol, bits) or None
                version = tls.version() or ""
                return {
                    "ok": True,
                    "version": version,
                    "cipher": cipher[0] if cipher else "",
                    "bits": cipher[2] if cipher else 0,
                }
    except Exception as exc:  # noqa: BLE001 — surface as protocol unsupported
        return {"ok": False, "error": str(exc)[:160]}


def _is_weak(cipher: str) -> bool:
    upper = (cipher or "").upper()
    return any(token in upper for token in _WEAK_TOKENS)


def run_tls_cipher_scan(
    url: str,
    *,
    port: int | None = None,
    log: Log = None,
) -> dict[str, Any]:
    host = hostname(url) if "://" in url else url.strip()
    if not host:
        raise ValueError("could not parse hostname")
    parsed = urlparse(url if "://" in url else f"https://{url}")
    port = int(port or parsed.port or 443)
    port = max(1, min(port, 65535))

    emit(log, f"tls_cipher_scan: {host}:{port}")
    negotiated = _try_connect(host, port, min_v=None, max_v=None)
    protocols: dict[str, Any] = {}
    for label, ver in _PROTOCOLS:
        if ver is None:
            protocols[label] = {"ok": False, "error": "not available in this Python/OpenSSL"}
            continue
        protocols[label] = _try_connect(host, port, min_v=ver, max_v=ver)

    issues: list[str] = []
    weak = False
    if negotiated.get("ok") and _is_weak(str(negotiated.get("cipher") or "")):
        weak = True
        issues.append(f"weak negotiated cipher: {negotiated.get('cipher')}")
    for label in ("TLSv1.0", "TLSv1.1"):
        if protocols.get(label, {}).get("ok"):
            issues.append(f"legacy protocol accepted: {label}")

    grade = "A"
    if any(protocols.get(p, {}).get("ok") for p in ("TLSv1.0", "TLSv1.1")):
        grade = "C"
    if weak:
        grade = "F" if grade == "C" else "D"
    if not negotiated.get("ok"):
        grade = "F"
        issues.append(f"TLS handshake failed: {negotiated.get('error', 'unknown')}")

    emit(log, f"tls_cipher_scan: grade={grade} cipher={negotiated.get('cipher')} issues={len(issues)}")
    return {
        "host": host,
        "port": port,
        "negotiated": negotiated,
        "protocols": protocols,
        "weak_cipher": weak,
        "grade": grade,
        "issues": issues,
    }
