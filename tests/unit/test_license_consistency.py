# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Argus is under the Zyvor Production License v1.0 only: no stale Apache/AGPL
headers or footers in first-party files."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".py", ".mjs", ".js", ".ts", ".rs", ".sh", ".j2", ".html", ".toml", ".md", ".yml", ".yaml"}
# Places that legitimately name other licences: history, the licensing explainer,
# and third-party data/tests about *other* software's licences.
ALLOWED = {
    "CHANGELOG.md", "docs/LICENSING.md", "NOTICE", "THIRD_PARTY.md",
    "tests/unit/test_license_consistency.py", "tests/unit/test_sca_scan.py",
}


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for rel in out.splitlines():
        path = ROOT / rel
        if rel in ALLOWED or path.suffix not in TEXT_SUFFIXES or not path.is_file():
            continue
        yield rel, path.read_text(encoding="utf-8", errors="ignore")


def test_no_apache_headers_in_first_party_files():
    offenders = [rel for rel, text in _tracked_text_files() if "Licensed under the Apache License" in text]
    assert offenders == []


def test_no_agpl_mentions_left():
    offenders = [rel for rel, text in _tracked_text_files() if "AGPL" in text]
    assert offenders == []


def test_login_footer_names_the_current_licence():
    html = (ROOT / "templates" / "login.html.j2").read_text(encoding="utf-8")
    assert "Zyvor Production License v1.0" in html


def test_package_manifests_point_at_the_licence_file():
    assert '"license": "SEE LICENSE IN LICENSE"' in (ROOT / "package.json").read_text()
    assert 'license-file = "../../LICENSE"' in (ROOT / "desktop/src-tauri/Cargo.toml").read_text()
