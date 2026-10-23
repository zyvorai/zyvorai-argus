# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""The desktop app sets ZYVOR_DESKTOP_MODE=true when it spawns `argus
serve` (desktop/src-tauri/src/server.rs) so the dashboard hides the
Kubernetes pods/workloads panel, which is always "cluster unavailable" for
a locally-wrapped app with no cluster of its own."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.webhook import create_app


def test_workloads_panel_hidden_in_desktop_mode(monkeypatch):
    monkeypatch.setenv("ZYVOR_DESKTOP_MODE", "true")
    client = TestClient(create_app())

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert 'id="workloads"' in response.text
    assert 'style="display:none"' in response.text


def test_workloads_panel_visible_by_default(monkeypatch):
    monkeypatch.delenv("ZYVOR_DESKTOP_MODE", raising=False)
    client = TestClient(create_app())

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert 'id="workloads"' in response.text
    # The wrapping <div> around the workloads/pods section carries the
    # display:none only in desktop mode; outside it, that section's own
    # opening tag is a bare <div> (other display:none spans exist elsewhere
    # in the page unrelated to this section, e.g. the events panel).
    workloads_section = response.text.split('id="workloads"')[0].splitlines()[-1]
    assert "display:none" not in workloads_section


def test_dashboard_accessibility_landmarks(monkeypatch):
    """Skip link, labelled modal palette and OS-theme-aware default."""
    monkeypatch.delenv("ZYVOR_DESKTOP_MODE", raising=False)
    client = TestClient(create_app())

    html = client.get("/dashboard").text

    assert 'href="#main-content"' in html and 'id="main-content"' in html
    assert 'role="dialog" aria-modal="true" aria-label="Command palette"' in html
    assert 'role="combobox"' in html and 'role="listbox"' in html
    assert "aria-current" in html
    assert "prefers-color-scheme: light" in html
