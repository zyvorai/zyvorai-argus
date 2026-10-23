# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""The header carries a labelled multi-target switcher backed by a dialog."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.webhook import create_app


def test_target_switcher_markup_and_script_are_rendered():
    html = TestClient(create_app()).get("/dashboard").text

    assert 'for="target-switch"' in html and 'id="target-switch"' in html
    assert '<dialog id="target-dialog"' in html and 'aria-labelledby="target-title"' in html
    assert "zyvor-targets" in html
