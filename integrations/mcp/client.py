# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Thin async HTTP client for Mission Control's `/api/v2` job API.

Deliberately has no knowledge of `orchestrator.*` internals — it only speaks
the public REST contract in `orchestrator/dashboard/v2_routes.py`, so this
package can be built, deployed, and versioned independently of the rest of
the pipeline.
"""

from __future__ import annotations

from typing import Any

import httpx


class ZyvorApiError(RuntimeError):
    """A non-2xx response from the Mission Control API."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ZyvorApiClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except ValueError:
                pass
            raise ZyvorApiError(response.status_code, str(detail))
        return response.json()

    async def enqueue_job(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": kind, "params": params}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/api/v2/jobs", json=payload)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v2/jobs/{job_id}")

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v2/jobs/{job_id}/cancel")

    async def list_findings(self, *, severity: str = "", limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if severity:
            params["severity"] = severity
        return await self._request("GET", "/api/v2/findings", params=params)

    async def list_schedules(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v2/schedules")
