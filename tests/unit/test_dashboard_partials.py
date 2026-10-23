# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""dashboard.html.j2 is assembled from templates/dashboard/ partials."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.webhook import create_app

PARTIALS = Path(__file__).resolve().parents[2] / "templates" / "dashboard"
PANELS = (
    "overview", "pipeline", "visual", "quality", "probes", "journeys",
    "api", "security", "requirements", "operations", "ask",
)  # fmt: skip


def test_every_panel_partial_exists_and_renders():
    html = TestClient(create_app()).get("/dashboard").text

    for name in PANELS:
        assert (PARTIALS / f"panel_{name}.html.j2").is_file()
        assert f'id="panel-{name}"' in html
    assert "<style>" in html and "const REFRESH =" in html
