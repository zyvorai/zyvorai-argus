# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Shared helpers for engagement-gated network-attack / DAST probes."""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

Log = Optional[Callable[[str], None]]


def client(insecure: bool = False, timeout: float = 15):
    import httpx

    return httpx.Client(timeout=timeout, verify=not insecure, follow_redirects=False)


def origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def hostname(url: str) -> str:
    return urlparse(url).hostname or ""


def with_query(url: str, updates: dict[str, str]) -> str:
    """Return url with query params merged/overwritten by ``updates``."""
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.update(updates)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


def emit(log: Log, message: str) -> None:
    if log:
        log(message)
