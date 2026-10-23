# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Test health can quarantine a flaky test and release it again."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.webhook import create_app


def test_test_health_wires_quarantine_and_release_endpoints():
    html = TestClient(create_app()).get("/dashboard").text

    assert "/api/v2/intel/quarantine" in html
    assert "data-quarantine" in html and "data-release" in html
    assert "ttl_hours: 72" in html
