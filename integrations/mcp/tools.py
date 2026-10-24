# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Pure, independently-testable tool logic behind the MCP server.

`server.py` only wires these into `@mcp.tool()` decorators — kept separate
so they can be unit tested against a fake `ZyvorApiClient` without spinning
up FastMCP or a real HTTP server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from integrations.mcp.allowlist import ALLOWED_KINDS
from integrations.mcp.client import ZyvorApiClient, ZyvorApiError

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

# Small, hand-maintained catalog for `list_job_kinds` — the authoritative
# per-kind param schema lives in orchestrator/dashboard/jobs.py:_validate(),
# but that module isn't importable here (this package has no orchestrator.*
# dependency by design), so this is kept intentionally short and descriptive
# rather than DRY with it.
_JOB_KIND_CATALOG: dict[str, str] = {
    "smoke": "Run the fixed Playwright smoke suite against the deployment's configured "
    "target. Takes no url — params: grep (str, optional), shard (e.g. '1/2', optional).",
    "audit": "Crawl a site and grade it A-F on accessibility/SEO/perf/security. params: url.",
    "crawl_test": "Crawl a site (BFS) and generate+run a test per discovered page. params: url.",
    "screenshot": "Full-page screenshots of a URL at chosen viewports. params: url.",
    "compare": "Visual diff between two URLs (e.g. staging vs prod). params: url_a, url_b.",
    "ping": "HTTP status + latency check across a list of URLs. params: urls (list[str]).",
    "tls": "DNS + TLS certificate inspection (issuer, expiry, protocol, SANs). params: host or url.",
    "vitals": "Core Web Vitals (LCP/CLS/INP/FCP/TTFB) measurement + grading. params: url.",
    "route_sweep": "Screenshot a set of routes at desktop/mobile, diff vs baselines. params: url.",
    "api_contract": "Validate REST endpoints against an OpenAPI spec. params: url.",
    "regression": "Run the manual suite with screenshot capture, diff vs baselines.",
    "discover": "Discover coverage inventory + gaps only (no generation/run).",
    "redirects": "Check a URL's redirect chain/status codes. params: url.",
    "headers": "Inspect a URL's HTTP response headers. params: url.",
    "cookies": "Inspect a URL's cookie attributes (Secure/HttpOnly/SameSite). params: url.",
    "robots": "Check a site's robots.txt. params: url.",
    "security_paths": "Probe common sensitive/security-relevant paths. params: url.",
    "api_check": "Hit an endpoint, assert status/JSON-path/contains. params: url, expect_status, "
    "json_path, contains.",
    "sitemap_test": "Validate a site's sitemap.xml. params: url.",
    "dns_records": "DNS record lookup. params: host.",
    "cors": "CORS header probe. params: url.",
    "transport": "Transport/security-header probe (HSTS etc.). params: url.",
}


def _error(exc: ZyvorApiError) -> dict[str, Any]:
    return {"error": exc.detail, "status_code": exc.status_code}


async def _await_completion(
    client: ZyvorApiClient, job: dict[str, Any], *, wait_s: float, poll_interval_s: float
) -> dict[str, Any]:
    job_id = job["id"]
    deadline = asyncio.get_event_loop().time() + wait_s
    current = job
    while current.get("status") not in TERMINAL_STATUSES:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return {
                "status": "running",
                "job_id": job_id,
                "note": f"still running after {wait_s:.0f}s — call get_job_status('{job_id}') to check back",
            }
        await asyncio.sleep(min(poll_interval_s, max(remaining, 0)))
        current = await client.get_job(job_id)
    return current


async def run_job(
    client: ZyvorApiClient,
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    wait_s: float,
    poll_interval_s: float,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if kind not in ALLOWED_KINDS:
        return {
            "error": f"kind '{kind}' is not allowed via this MCP server. "
            f"Allowed: {', '.join(sorted(ALLOWED_KINDS))}"
        }
    try:
        job = await client.enqueue_job(kind, params or {}, idempotency_key=idempotency_key)
        return await _await_completion(client, job, wait_s=wait_s, poll_interval_s=poll_interval_s)
    except ZyvorApiError as exc:
        return _error(exc)


async def run_smoke_test(
    client: ZyvorApiClient, *, wait_s: float, poll_interval_s: float
) -> dict[str, Any]:
    """Runs the fixed smoke suite against the deployment's configured target.

    Does NOT accept a url — the `smoke` job kind always tests the server's
    own configured ZYVOR_BASE_URL. For an arbitrary site, use
    run_site_audit or run_crawl_test instead.
    """
    return await run_job(client, "smoke", {}, wait_s=wait_s, poll_interval_s=poll_interval_s)


async def run_site_audit(
    client: ZyvorApiClient, url: str, *, wait_s: float, poll_interval_s: float
) -> dict[str, Any]:
    return await run_job(client, "audit", {"url": url}, wait_s=wait_s, poll_interval_s=poll_interval_s)


async def run_crawl_test(
    client: ZyvorApiClient, url: str, *, wait_s: float, poll_interval_s: float
) -> dict[str, Any]:
    return await run_job(
        client, "crawl_test", {"url": url}, wait_s=wait_s, poll_interval_s=poll_interval_s
    )


async def get_job_status(client: ZyvorApiClient, job_id: str) -> dict[str, Any]:
    try:
        return await client.get_job(job_id)
    except ZyvorApiError as exc:
        return _error(exc)


async def cancel_job(client: ZyvorApiClient, job_id: str) -> dict[str, Any]:
    try:
        return await client.cancel_job(job_id)
    except ZyvorApiError as exc:
        return _error(exc)


async def list_job_kinds() -> dict[str, Any]:
    return {"kinds": dict(sorted(_JOB_KIND_CATALOG.items()))}


_SEVERITIES = {"critical", "high", "medium", "low", "info"}


async def get_findings(client: ZyvorApiClient, severity: str = "", limit: int = 50) -> dict[str, Any]:
    """Open findings, newest first, with per-severity counts. Read-only."""
    severity = severity.strip().lower()
    if severity and severity not in _SEVERITIES:
        return {"error": f"severity must be one of {sorted(_SEVERITIES)}", "status_code": 400}
    try:
        return await client.list_findings(severity=severity, limit=max(1, min(int(limit), 200)))
    except ZyvorApiError as exc:
        return _error(exc)


async def list_schedules(client: ZyvorApiClient) -> dict[str, Any]:
    """Recurring jobs Mission Control runs on a schedule. Read-only."""
    try:
        return await client.list_schedules()
    except ZyvorApiError as exc:
        return _error(exc)
