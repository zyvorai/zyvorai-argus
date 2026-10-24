# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""MCP server entry point. Registers thin tool wrappers around integrations.mcp.tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from integrations.mcp import tools as job_tools
from integrations.mcp.client import ZyvorApiClient
from integrations.mcp.config import MCPConfig, load_config


def build_server(config: MCPConfig) -> FastMCP:
    mcp = FastMCP("argus", host=config.host, port=config.port)
    client = ZyvorApiClient(config.base_url, config.api_token)

    def _wait_s(wait_s: float | None) -> float:
        if wait_s is None:
            return config.default_wait_s
        return max(0.0, min(wait_s, config.max_wait_s))

    @mcp.tool()
    async def run_job(kind: str, params: dict[str, Any] | None = None, wait_s: float | None = None) -> dict[str, Any]:
        """Trigger a Mission Control QA job and wait (bounded) for it to finish.

        kind must be one of the kinds returned by list_job_kinds. If the job
        doesn't finish within the wait budget, returns {"status": "running",
        "job_id": ...} — call get_job_status(job_id) to check back later.
        """
        return await job_tools.run_job(
            client, kind, params, wait_s=_wait_s(wait_s), poll_interval_s=config.poll_interval_s
        )

    @mcp.tool()
    async def run_smoke_test(wait_s: float | None = None) -> dict[str, Any]:
        """Run the fixed smoke suite against the deployment's configured target.

        Does NOT take a url — smoke always tests the server's own configured
        target. Use run_site_audit(url) or run_crawl_test(url) for an
        arbitrary site.
        """
        return await job_tools.run_smoke_test(
            client, wait_s=_wait_s(wait_s), poll_interval_s=config.poll_interval_s
        )

    @mcp.tool()
    async def run_site_audit(url: str, wait_s: float | None = None) -> dict[str, Any]:
        """Crawl `url` and grade it A-F on accessibility/SEO/performance/security."""
        return await job_tools.run_site_audit(
            client, url, wait_s=_wait_s(wait_s), poll_interval_s=config.poll_interval_s
        )

    @mcp.tool()
    async def run_crawl_test(url: str, wait_s: float | None = None) -> dict[str, Any]:
        """Crawl `url` and generate+run a Playwright test per discovered page."""
        return await job_tools.run_crawl_test(
            client, url, wait_s=_wait_s(wait_s), poll_interval_s=config.poll_interval_s
        )

    @mcp.tool()
    async def get_job_status(job_id: str) -> dict[str, Any]:
        """Check the status/result of a previously started job."""
        return await job_tools.get_job_status(client, job_id)

    @mcp.tool()
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running job."""
        return await job_tools.cancel_job(client, job_id)

    @mcp.tool()
    async def list_job_kinds() -> dict[str, Any]:
        """List the job kinds runnable via run_job, with a one-line description each."""
        return await job_tools.list_job_kinds()

    @mcp.tool()
    async def get_findings(severity: str = "", limit: int = 50) -> dict[str, Any]:
        """List open findings (newest first) with per-severity counts.

        severity optionally filters to one of critical/high/medium/low/info.
        """
        return await job_tools.get_findings(client, severity, limit)

    @mcp.tool()
    async def list_schedules() -> dict[str, Any]:
        """List the recurring jobs Mission Control runs on a schedule."""
        return await job_tools.list_schedules(client)

    @mcp.resource("zyvor://findings/open")
    async def open_findings() -> dict[str, Any]:
        """Ambient context: the current open findings and their severity counts."""
        return await job_tools.get_findings(client, "", 100)

    @mcp.resource("zyvor://schedules")
    async def schedules() -> dict[str, Any]:
        """Ambient context: the configured recurring jobs."""
        return await job_tools.list_schedules(client)

    return mcp


def main() -> None:
    config = load_config()
    mcp = build_server(config)
    mcp.run(transport=config.transport)


if __name__ == "__main__":
    main()
