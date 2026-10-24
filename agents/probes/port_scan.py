# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Bounded TCP connect scan of common service ports — detection only.

No SYN floods, no UDP blast, no masscan-style sweeps. Caps port count and
per-port timeout. Gated at the ``active_recon`` engagement tier.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

from agents.probes._scan_common import Log, emit, hostname

# Curated common-service list — deliberately small (not a full 1–65535 sweep).
DEFAULT_PORTS: tuple[int, ...] = (
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445, 465, 587,
    993, 995, 1433, 1521, 2049, 3306, 3389, 5432, 5900, 6379, 8080, 8443,
    9200, 11211, 27017,
)


def _probe_port(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        result = sock.connect_ex((host, port))
        open_ = result == 0
    except OSError as exc:
        return {"port": port, "open": False, "error": str(exc)[:120]}
    finally:
        sock.close()
    return {"port": port, "open": open_}


def run_port_scan(
    url: str,
    *,
    ports: list[int] | None = None,
    timeout_s: float = 1.0,
    max_workers: int = 16,
    log: Log = None,
) -> dict[str, Any]:
    """TCP connect-scan ``ports`` against the host in ``url`` (or bare host)."""
    host = hostname(url) if "://" in url else (urlparse(f"//{url}").hostname or url.strip())
    if not host:
        raise ValueError("could not parse hostname from url")
    selected = list(ports) if ports else list(DEFAULT_PORTS)
    # Hard caps — never a full port range from params.
    selected = sorted({max(1, min(int(p), 65535)) for p in selected})[:64]
    timeout_s = max(0.2, min(float(timeout_s), 3.0))
    max_workers = max(1, min(int(max_workers), 32))

    emit(log, f"port_scan: {host} ({len(selected)} ports, timeout={timeout_s}s)")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe_port, host, port, timeout_s): port for port in selected}
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r["port"])
    open_ports = [r["port"] for r in results if r.get("open")]
    emit(log, f"port_scan: {len(open_ports)} open — {open_ports}")
    return {
        "host": host,
        "scanned": len(selected),
        "open_ports": open_ports,
        "results": results,
        "issues": [f"open port {p}" for p in open_ports],
    }
