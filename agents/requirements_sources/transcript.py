# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Meeting transcript (.vtt / .srt / .txt) → requirement text."""

from __future__ import annotations

import re
from pathlib import Path

_SUPPORTED = {".vtt", ".srt", ".txt", ".md"}


def _strip_vtt(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("WEBVTT"):
            continue
        if re.match(r"^\d+$", s):
            continue
        if re.match(r"^\d{2}:\d{2}", s) or "-->" in s:
            continue
        lines.append(s)
    return "\n".join(lines)


def _strip_srt(text: str) -> str:
    return _strip_vtt(text)


def load_transcript(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        body = _strip_vtt(raw)
    elif suffix == ".srt":
        body = _strip_srt(raw)
    else:
        body = raw.strip()
    title = path.stem.replace("_", " ").replace("-", " ").strip() or path.stem
    return f"# Meeting transcript: {title}\n\n{body}\n"


def load_paths(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    contents: list[str] = []
    used: list[str] = []
    errors: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            errors.append(f"{raw}: not found")
            continue
        if p.suffix.lower() not in _SUPPORTED:
            errors.append(f"{raw}: unsupported transcript type (use .vtt/.srt/.txt/.md)")
            continue
        try:
            contents.append(load_transcript(p))
            used.append(str(p))
        except Exception as exc:
            errors.append(f"{raw}: {exc}")
    return contents, used, errors
