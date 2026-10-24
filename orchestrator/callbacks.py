# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Outbound job-completion callbacks.

A job enqueued through ``POST /api/v2/jobs`` may carry a ``callback_url``. When
the job reaches a terminal state, Argus POSTs a small signed JSON notice to it,
so a chat agent or CI system is pushed the result instead of polling.

Safety properties (this is an SSRF surface, so they are deliberate):

* Off unless ``ZYVOR_CALLBACKS_ENABLED=true``.
* The destination must pass a *callback-specific* policy -- not the QA target
  allowlist: HTTPS (HTTP only outside production), port 443 unless extended,
  and a host in ``ZYVOR_CALLBACK_ALLOWLIST`` (``hooks.slack.com`` is always
  allowed). Private/loopback/metadata addresses are refused unless
  ``ZYVOR_CALLBACK_ALLOW_PRIVATE=true``.
* The policy runs at enqueue time and again immediately before each send. The
  connection itself is not pinned to the validated IP, so a DNS-rebinding race
  between that check and the connect is narrowed, not eliminated.
* Redirects are never followed; the timeout is short and the response body is
  not read.
* The payload carries only id, kind, status and (optionally) a link. Never the
  job's params, result or error text.
* Non-Slack destinations require ``ZYVOR_CALLBACK_SECRET`` and receive an
  ``X-Zyvor-Signature: sha256=<hmac>`` over ``"<timestamp>.<body>"``, with the
  timestamp in ``X-Zyvor-Timestamp`` so receivers can reject replays.
* The URL is stored in its own column and is never returned by the API or
  written to the audit log (it may carry a token in the query string); audit
  rows record only the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import httpx

from orchestrator.security.target_policy import TargetPolicy, TargetPolicyError

log = logging.getLogger(__name__)


class _RedactQuery(logging.Filter):
    """httpx logs every request URL at INFO, query string included; a callback
    URL may carry a token there, so strip queries from its log lines."""

    _QUERY = re.compile(r"\?[^\s\"']*")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._QUERY.sub("?[redacted]", record.getMessage())
        record.args = None
        return True


logging.getLogger("httpx").addFilter(_RedactQuery())

SLACK_HOST = "hooks.slack.com"
_TERMINAL = {"succeeded", "failed", "cancelled"}
_pool: ThreadPoolExecutor | None = None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def enabled() -> bool:
    return _truthy("ZYVOR_CALLBACKS_ENABLED")


def policy() -> TargetPolicy:
    production = os.environ.get("ZYVOR_ENV", "development").lower() == "production"
    hosts = {h.lower().rstrip(".") for h in _csv("ZYVOR_CALLBACK_ALLOWLIST")} | {SLACK_HOST}
    ports = tuple(int(p) for p in _csv("ZYVOR_CALLBACK_ALLOWED_PORTS") or ["443"])
    return TargetPolicy(
        allowed_hosts=tuple(sorted(hosts)),
        allowed_ports=ports,
        allow_private=_truthy("ZYVOR_CALLBACK_ALLOW_PRIVATE"),
        allow_http=_truthy("ZYVOR_CALLBACK_ALLOW_HTTP") and not production,
        resolve_dns=not _truthy("ZYVOR_SKIP_TARGET_DNS_RESOLUTION"),
    )


def _is_slack(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == SLACK_HOST


def validate_callback_url(url: str) -> str:
    """Validate a callback URL at enqueue time; raises ``ValueError`` subclasses."""
    if not enabled():
        raise ValueError("job callbacks are disabled (set ZYVOR_CALLBACKS_ENABLED=true)")
    try:
        clean = policy().validate_url(url)
    except TargetPolicyError as exc:
        raise TargetPolicyError(f"callback_url rejected: {exc}") from exc
    if not _is_slack(clean) and not os.environ.get("ZYVOR_CALLBACK_SECRET"):
        raise ValueError("callback_url needs ZYVOR_CALLBACK_SECRET to sign the notice")
    return clean


def build_payload(job_id: str, kind: str, status: str, duration_s: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": "job.finished", "job_id": job_id, "kind": kind, "status": status}
    if duration_s is not None:
        payload["duration_s"] = round(duration_s, 3)
    base = os.environ.get("ZYVOR_PUBLIC_URL", "").rstrip("/")
    if base:
        payload["link"] = f"{base}/api/v2/jobs/{job_id}"
    return payload


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _request(url: str, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    if _is_slack(url):
        text = f"Argus job `{payload['kind']}` {payload['status']} ({payload['job_id']})"
        if payload.get("link"):
            text += f" {payload['link']}"
        return json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Zyvor-Timestamp": timestamp,
        "X-Zyvor-Signature": sign(os.environ.get("ZYVOR_CALLBACK_SECRET", ""), timestamp, body),
    }
    return body, headers


def deliver(
    url: str,
    payload: dict[str, Any],
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
    timeout_s: float = 5.0,
    client: httpx.Client | None = None,
) -> tuple[bool, str]:
    """POST ``payload`` to ``url`` with retries. Returns ``(ok, detail)``; never raises."""
    detail = "not attempted"
    for attempt in range(1, attempts + 1):
        try:
            clean = policy().validate_url(url)  # re-check right before connecting
            body, headers = _request(clean, payload)
            owns = client is None
            http = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
            try:
                with http.stream("POST", clean, content=body, headers=headers) as resp:
                    code = resp.status_code
            finally:
                if owns:
                    http.close()
            if 200 <= code < 300:
                return True, f"HTTP {code}"
            detail = f"HTTP {code}"
            if code < 500 and code != 429:
                return False, detail  # a 4xx will not get better by retrying
        except TargetPolicyError as exc:
            return False, f"blocked: {exc}"
        except Exception as exc:  # noqa: BLE001 - delivery must never raise into the worker
            detail = f"{type(exc).__name__}"
        if attempt < attempts:
            time.sleep(backoff_s * (4 ** (attempt - 1)))
    return False, detail


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zyvor-callback")
    return _pool


def dispatch(store: Any, job_id: str, kind: str, status: str, duration_s: float | None = None) -> None:
    """Queue a completion callback for ``job_id`` if it asked for one. Never raises."""
    try:
        if status not in _TERMINAL or not enabled():
            return
        url = store.get_job_callback(job_id)
        if not url:
            return
        payload = build_payload(job_id, kind, status, duration_s)
        _executor().submit(_run, store, job_id, url, payload)
    except Exception:  # noqa: BLE001
        log.exception("could not queue callback for job %s", job_id)


def _run(store: Any, job_id: str, url: str, payload: dict[str, Any]) -> None:
    ok, detail = deliver(url, payload)
    try:
        store.audit(
            "job.callback",
            actor="system",
            resource_type="job",
            resource_id=job_id,
            outcome="success" if ok else "failure",
            detail={"host": urlsplit(url).hostname, "result": detail},
        )
    except Exception:  # noqa: BLE001
        log.exception("could not audit callback for job %s", job_id)
