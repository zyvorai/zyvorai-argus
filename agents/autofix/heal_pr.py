# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Helpers for turning a confirmed autofix into a draft pull request.

Pure functions only (repo discovery, branch/body text, file collection); the
GitHub calls live in ``GitHubClient.create_draft_pr`` and the node that wires
them together in ``orchestrator/nodes/heal_pr.py``.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from agents.common.models import AutofixSuggestion

_REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def enabled() -> bool:
    return os.environ.get("ENABLE_HEAL_PR", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_repo(root: Path) -> str:
    """``owner/name``: ``ARGUS_HEAL_PR_REPO``, then ``GITHUB_REPOSITORY``, then ``origin``."""
    for name in ("ARGUS_HEAL_PR_REPO", "GITHUB_REPOSITORY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        url = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _REMOTE_RE.search(url)
    return match.group(1) if match else ""


def collect_files(patched_files: list[str], root: Path) -> tuple[dict[str, str], list[str]]:
    """Map repo-relative path -> patched text. Files outside ``root`` are rejected."""
    root = root.resolve()
    files: dict[str, str] = {}
    rejected: list[str] = []
    for raw in dict.fromkeys(patched_files):  # de-duplicate, keep order
        path = Path(raw).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rejected.append(raw)
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except OSError:
            rejected.append(raw)
    return files, rejected


def branch_name(files: dict[str, str], run_id: str | None) -> str:
    """Stable per content, so a re-run of the same fix never opens a second branch."""
    digest = hashlib.sha256("\n".join(f"{k}\0{v}" for k, v in sorted(files.items())).encode()).hexdigest()[:8]
    return f"argus/heal-{run_id or 'local'}-{digest}"


def build_title(files: dict[str, str]) -> str:
    names = sorted(Path(p).name for p in files)
    shown = ", ".join(names[:2]) + (f" and {len(names) - 2} more" if len(names) > 2 else "")
    return f"fix(tests): repair locators in {shown}"


def _cell(text: str, *, code: bool = False) -> str:
    """One Markdown table cell: single line, pipes escaped, bounded length."""
    text = " ".join(str(text).split())[:200].replace("|", "\\|")
    if code:
        text = text.replace("`", "'")
        return f"`{text}`"
    return text


def build_body(suggestions: list[AutofixSuggestion], run_id: str | None) -> str:
    lines = [
        "Argus repaired locators in these specs, re-ran the tests, and they passed.",
        "",
        "**This is a draft: review each change.** Argus swaps a selector with a text match "
        "(first occurrence only), so confirm the new locator targets the same element.",
        "",
        "| Test | Old locator | New locator | Why |",
        "|---|---|---|---|",
    ]
    for s in suggestions:
        lines.append(
            f"| {_cell(s.test_title)} | {_cell(s.original_selector, code=True)} "
            f"| {_cell(s.suggested_selector, code=True)} | {_cell(s.explanation)} |"
        )
    if run_id:
        lines += ["", f"Argus run: `{run_id}`"]
    return "\n".join(lines)
