# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Rust-powered screenshot diff bridge."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from agents.common.models import RegressionDiff
from orchestrator.paths import repo_root

_TIMEOUT_SECONDS = 120


def rust_diff_available() -> bool:
    return _find_binary() is not None


def _is_usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _find_binary() -> Path | None:
    # An explicit ZYVOR_DIFF_BINARY override wins; an unset/empty value must be
    # ignored (Path("") is "." and would match the current directory).
    override = os.environ.get("ZYVOR_DIFF_BINARY", "").strip()
    if override:
        p = Path(override)
        if _is_usable(p):
            return p
    repo = repo_root()
    for c in (
        repo / "rust" / "target" / "release" / "zyvor-diff",
        repo / "rust" / "target" / "debug" / "zyvor-diff",
    ):
        if _is_usable(c):
            return c
    return None


def compare_with_rust(
    baseline: Path,
    current: Path,
    diff_output: Path,
    threshold: float = 1.0,
) -> RegressionDiff:
    """Run Rust zyvor-diff binary for fast image comparison."""
    binary = _find_binary()
    if not binary:
        return RegressionDiff(
            file=current.name,
            status="fail",
            message="Rust zyvor-diff binary not found. Run: cd rust && cargo build --release",
        )

    try:
        result = _run_diff(binary, baseline, current, diff_output, threshold)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RegressionDiff(
            file=current.name,
            status="fail",
            message=f"Rust diff failed to run: {exc}",
        )
    return _parse_result(result, current, diff_output)


def _run_diff(binary, baseline, current, diff_output, threshold):
    return subprocess.run(
        [
            str(binary),
            "--baseline", str(baseline),
            "--current", str(current),
            "--diff-output", str(diff_output),
            "--threshold", str(threshold),
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )


def _parse_result(result, current: Path, diff_output: Path) -> RegressionDiff:
    try:
        data = json.loads(result.stdout.strip().split("\n")[-1])
        return RegressionDiff(
            file=current.name,
            status="pass" if data["passed"] else "fail",
            diff_percent=data["diff_percent"],
            diff_image_path=str(diff_output),
            message=None if data["passed"] else f"Rust diff {data['diff_percent']:.2f}%",
        )
    except (json.JSONDecodeError, KeyError, IndexError):
        return RegressionDiff(
            file=current.name,
            status="fail",
            message=result.stderr or "Rust diff failed",
        )
