# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""ZYVOR_HOME gives a read-only app bundle a writable workspace."""

from __future__ import annotations

from orchestrator import paths


def test_workspace_links_assets_and_creates_output_dirs(tmp_path):
    bundle, home = tmp_path / "bundle", tmp_path / "home"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "package.json").write_text("{}")

    paths.ensure_workspace(home, bundle)

    assert (home / "templates").is_symlink() and (home / "templates").resolve() == (
        bundle / "templates"
    ).resolve()
    assert (home / "package.json").read_text() == "{}"
    assert (home / "reports").is_dir() and not (home / "reports").is_symlink()
    assert (home / "tests" / "generated").is_dir()
    assert not (home / "prompts").exists()  # absent in the bundle -> not linked


def test_workspace_is_idempotent_and_repoints_stale_links(tmp_path):
    old, new, home = tmp_path / "old", tmp_path / "new", tmp_path / "home"
    (old / "templates").mkdir(parents=True)
    (new / "templates").mkdir(parents=True)
    paths.ensure_workspace(home, old)
    (home / "reports" / "keep.txt").write_text("x")

    paths.ensure_workspace(home, new)
    paths.ensure_workspace(home, new)

    assert (home / "templates").resolve() == (new / "templates").resolve()
    assert (home / "reports" / "keep.txt").exists()


def test_repo_root_honours_zyvor_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZYVOR_HOME", str(tmp_path / "ws"))

    assert paths.repo_root() == tmp_path / "ws"
    assert (tmp_path / "ws" / "reports").is_dir()


def test_repo_root_defaults_to_checkout(monkeypatch):
    monkeypatch.delenv("ZYVOR_HOME", raising=False)

    assert (paths.repo_root() / "orchestrator").is_dir()
