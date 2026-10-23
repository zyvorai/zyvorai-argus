# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""The Operations panel ships a labelled baseline/candidate run comparison."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.webhook import create_app


def test_compare_runs_panel_is_rendered_and_labelled():
    html = TestClient(create_app()).get("/dashboard").text

    assert 'id="cmp-a"' in html and 'aria-label="Baseline run"' in html
    assert 'id="cmp-b"' in html and 'aria-label="Candidate run"' in html
    assert 'id="cmp-out"' in html and "renderCompare(runs)" in html
