# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Tests for the Rust diff bridge binary lookup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.regression import rust_bridge


@pytest.fixture
def no_builds(monkeypatch, tmp_path):
    monkeypatch.setattr(rust_bridge, "repo_root", lambda: tmp_path)
    return tmp_path


def test_unset_env_does_not_match_cwd(monkeypatch, no_builds):
    monkeypatch.delenv("ZYVOR_DIFF_BINARY", raising=False)
    assert rust_bridge._find_binary() is None
    assert rust_bridge.rust_diff_available() is False


def test_empty_env_ignored(monkeypatch, no_builds):
    monkeypatch.setenv("ZYVOR_DIFF_BINARY", "")
    assert rust_bridge._find_binary() is None


def test_env_override_beats_target_build(monkeypatch, no_builds):
    built = no_builds / "rust" / "target" / "release" / "zyvor-diff"
    built.parent.mkdir(parents=True)
    built.write_text("#!/bin/sh\n")
    built.chmod(0o755)
    custom = no_builds / "custom-diff"
    custom.write_text("#!/bin/sh\n")
    custom.chmod(0o755)
    monkeypatch.setenv("ZYVOR_DIFF_BINARY", str(custom))
    assert rust_bridge._find_binary() == custom


def test_directory_or_non_executable_rejected(monkeypatch, no_builds):
    monkeypatch.setenv("ZYVOR_DIFF_BINARY", str(no_builds))
    assert rust_bridge._find_binary() is None
    f = no_builds / "x"
    f.write_text("")
    f.chmod(0o644)
    monkeypatch.setenv("ZYVOR_DIFF_BINARY", str(f))
    assert rust_bridge._find_binary() is None


def test_run_failure_returns_fail_diff(monkeypatch, no_builds):
    b = no_builds / "bin"
    b.write_text("")
    b.chmod(0o755)
    monkeypatch.setenv("ZYVOR_DIFF_BINARY", str(b))

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(rust_bridge.subprocess, "run", boom)
    r = rust_bridge.compare_with_rust(Path("a.png"), Path("b.png"), Path("d.png"))
    assert r.status == "fail"
