# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Meeting audio / diarized transcript → requirement text.

Supports:
- Speaker-tagged WebVTT (`<v Name>…`) already on disk
- Optional live diarization via `ZYVOR_DIARIZE_CMD` (shell template with `{input}`
  / `{output}`) or `ZYVOR_DIARIZE_API_URL` (POST multipart audio → text/VTT)
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from agents.requirements_sources.transcript import load_transcript

_AUDIO = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm"}
_VTT_VOICE = re.compile(r"<v\s+([^>]+)>(.*)$", re.IGNORECASE)


def format_speaker_lines(text: str) -> str:
    """Normalize WebVTT voice tags into `Speaker: line` markdown."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        m = _VTT_VOICE.search(s)
        if m:
            out.append(f"{m.group(1).strip()}: {m.group(2).strip()}")
        else:
            out.append(s)
    return "\n".join(out)


def _run_cmd_diarize(audio: Path) -> str:
    template = (os.environ.get("ZYVOR_DIARIZE_CMD") or "").strip()
    if not template:
        raise RuntimeError("ZYVOR_DIARIZE_CMD not set")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.vtt"
        cmd = template.format(input=str(audio), output=str(out))
        subprocess.run(cmd, shell=True, check=True, timeout=600)  # nosec B602 — operator-owned cmd
        if not out.exists():
            raise RuntimeError("diarize command produced no output file")
        return format_speaker_lines(out.read_text(encoding="utf-8", errors="replace"))


def _run_api_diarize(audio: Path) -> str:
    url = (os.environ.get("ZYVOR_DIARIZE_API_URL") or "").strip()
    if not url:
        raise RuntimeError("ZYVOR_DIARIZE_API_URL not set")
    boundary = "----zyvorDiarizeBoundary7MA4YWxk"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "text/plain, text/vtt, application/json",
        },
    )
    token = (os.environ.get("ZYVOR_DIARIZE_API_TOKEN") or "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
        raw = resp.read().decode("utf-8", errors="replace")
    return format_speaker_lines(raw)


def diarize_audio(path: Path) -> str:
    if (os.environ.get("ZYVOR_DIARIZE_CMD") or "").strip():
        body = _run_cmd_diarize(path)
    elif (os.environ.get("ZYVOR_DIARIZE_API_URL") or "").strip():
        body = _run_api_diarize(path)
    else:
        raise RuntimeError(
            "Audio diarization needs ZYVOR_DIARIZE_CMD or ZYVOR_DIARIZE_API_URL "
            "(or pass a speaker-tagged .vtt/.srt instead)"
        )
    title = path.stem.replace("_", " ").replace("-", " ").strip() or path.stem
    return f"# Meeting (diarized): {title}\n\n{body}\n"


def load_paths(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    contents: list[str] = []
    used: list[str] = []
    errors: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            errors.append(f"{raw}: not found")
            continue
        suffix = p.suffix.lower()
        try:
            if suffix in _AUDIO:
                contents.append(diarize_audio(p))
                used.append(str(p))
            elif suffix in {".vtt", ".srt", ".txt", ".md"}:
                text = load_transcript(p)
                # Re-title + enrich speaker tags when present
                body = format_speaker_lines(text)
                contents.append(body if body.startswith("#") else f"# Meeting transcript\n\n{body}\n")
                used.append(str(p))
            else:
                errors.append(f"{raw}: unsupported type for diarize source")
        except Exception as exc:
            errors.append(f"{raw}: {exc}")
    return contents, used, errors
