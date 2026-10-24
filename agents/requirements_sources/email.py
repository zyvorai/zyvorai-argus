# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Email (.eml) → requirement text."""

from __future__ import annotations

import email
import email.policy
from pathlib import Path


def load_eml(path: Path) -> str:
    """Extract subject + plain-text body from a single .eml file."""
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("subject") or path.stem).strip()
    body_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body_parts.append(payload.decode("utf-8", errors="replace"))
            elif ctype == "text/html" and not body_parts:
                try:
                    html = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    html = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
                # Crude strip — enough for requirement parsing; not a browser.
                import re

                body_parts.append(re.sub(r"<[^>]+>", " ", html))
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                body_parts.append(payload.decode("utf-8", errors="replace"))
            else:
                body_parts.append(str(msg.get_payload() or ""))
    body = "\n".join(p.strip() for p in body_parts if p and str(p).strip()).strip()
    return f"# {subject}\n\n{body}\n"


def load_paths(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Returns (spec_contents, spec_paths, errors)."""
    contents: list[str] = []
    used: list[str] = []
    errors: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            errors.append(f"{raw}: not found")
            continue
        if p.suffix.lower() != ".eml":
            errors.append(f"{raw}: expected .eml")
            continue
        try:
            contents.append(load_eml(p))
            used.append(str(p))
        except Exception as exc:
            errors.append(f"{raw}: {exc}")
    return contents, used, errors


def fetch_imap(
    *,
    folder: str | None = None,
    limit: int = 20,
    subject_contains: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Pull recent messages from IMAP (Gmail app password / mailbox).

    Env: IMAP_HOST (default imap.gmail.com), IMAP_USER|GMAIL_USER,
    IMAP_PASSWORD|GMAIL_APP_PASSWORD, optional IMAP_FOLDER.
    """
    import email as email_lib
    import email.policy
    import imaplib
    import os

    host = (os.environ.get("IMAP_HOST") or "imap.gmail.com").strip()
    user = (os.environ.get("IMAP_USER") or os.environ.get("GMAIL_USER") or "").strip()
    password = (
        os.environ.get("IMAP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD") or ""
    ).strip()
    folder = (folder or os.environ.get("IMAP_FOLDER") or "INBOX").strip() or "INBOX"
    limit = max(1, min(int(limit), 100))
    if not user or not password:
        return [], [], ["IMAP_USER/GMAIL_USER and IMAP_PASSWORD/GMAIL_APP_PASSWORD required"]

    contents: list[str] = []
    labels: list[str] = []
    errors: list[str] = []
    try:
        client = imaplib.IMAP4_SSL(host, timeout=30)
        try:
            client.login(user, password)
            typ, _ = client.select(folder, readonly=True)
            if typ != "OK":
                return [], [], [f"IMAP select {folder!r} failed"]
            criteria = "ALL"
            if subject_contains:
                safe = subject_contains.replace('"', "")
                criteria = f'(SUBJECT "{safe}")'
            typ, data = client.search(None, criteria)
            if typ != "OK" or not data or not data[0]:
                return [], [], []
            ids = data[0].split()
            for msg_id in ids[-limit:]:
                typ, fetched = client.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email_lib.message_from_bytes(bytes(raw), policy=email.policy.default)
                # Reuse .eml path via a temp-less in-memory extract
                subject = str(msg.get("subject") or f"imap-{msg_id.decode()}").strip()
                body_parts: list[str] = []
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body_parts.append(part.get_content())
                            except Exception:
                                payload = part.get_payload(decode=True)
                                if isinstance(payload, bytes):
                                    body_parts.append(payload.decode("utf-8", errors="replace"))
                else:
                    try:
                        body_parts.append(msg.get_content())
                    except Exception:
                        payload = msg.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            body_parts.append(payload.decode("utf-8", errors="replace"))
                body = "\n".join(p.strip() for p in body_parts if p and str(p).strip()).strip()
                contents.append(f"# {subject}\n\n{body}\n")
                labels.append(f"imap:{folder}:{msg_id.decode(errors='replace')}")
        finally:
            try:
                client.logout()
            except Exception:
                pass
    except Exception as exc:
        errors.append(str(exc))
    return contents, labels, errors
