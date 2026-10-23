# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Frozen-binary-aware repo-root resolution.

Every module that needs to locate `templates/`, `prompts/`, `tests/`,
`reports/`, etc. relative to the repo root should call `repo_root()` here
instead of hand-rolling `Path(__file__).resolve().parents[N]` locally --
that pattern assumes a real filesystem checkout and silently resolves to
the wrong directory (or one that doesn't exist at all) inside a
PyInstaller-frozen binary, since a frozen build's `__file__` points into a
one-off temp extraction directory, not the source tree.

See ROADMAP.md's "Desktop app v2" section -- this is the code-side half of
what a real single-binary freeze needs; Playwright's browser binaries not
being freezable is the other, unrelated half.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Bundle assets the app only reads; linked into the writable workspace.
_READ_ONLY_ASSETS = (
    "templates",
    "prompts",
    "playwright",
    "node_modules",
    "package.json",
    "package-lock.json",
)
# Directories the app writes into.
_WRITABLE_DIRS = ("reports", "screenshots", "tests/manual", "tests/generated", "tests/fixtures")


def bundle_root() -> Path:
    """Where the read-only assets live (source checkout or frozen bundle)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def ensure_workspace(home: Path, bundle: Path) -> Path:
    """Materialize a writable workspace for a read-only app bundle.

    Reports, history and generated tests are written relative to the repo
    root throughout the code base. Inside a signed .app that location is
    read-only, so the desktop shell points ZYVOR_HOME at a directory under
    Application Support: read-only assets are symlinked from the bundle and
    the output directories are real. Idempotent; stale links are repointed.
    """
    home.mkdir(parents=True, exist_ok=True)
    for name in _WRITABLE_DIRS:
        (home / name).mkdir(parents=True, exist_ok=True)
    for name in _READ_ONLY_ASSETS:
        src, dst = bundle / name, home / name
        if not src.exists():
            continue
        if dst.is_symlink() and dst.resolve() != src.resolve():
            dst.unlink()
        if not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src)
    return home


def repo_root() -> Path:
    home = os.environ.get("ZYVOR_HOME")
    if home:
        return ensure_workspace(Path(home).expanduser(), bundle_root())
    if getattr(sys, "frozen", False):
        # PyInstaller sets `sys.frozen = True` and, for a onefile build,
        # `sys._MEIPASS` to the temp dir bundled data was extracted into
        # (onedir builds don't set _MEIPASS; the executable's own directory
        # plays the same role there). Either way, that IS "repo root" for a
        # frozen build -- packaging is expected to bundle templates/prompts/
        # etc. at that same top level, mirroring docker/Dockerfile's layout.
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]
