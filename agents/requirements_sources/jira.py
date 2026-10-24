# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Jira-like ticket source: JSON export files and optional REST API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _issue_to_markdown(issue: dict[str, Any]) -> str:
    key = str(issue.get("key") or issue.get("id") or "TICKET")
    raw_fields = issue.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else issue
    summary = str(fields.get("summary") or fields.get("title") or key)
    description = fields.get("description") or fields.get("body") or ""
    if isinstance(description, dict):
        # Atlassian document format — flatten text nodes crudely
        description = _adf_to_text(description)
    labels = fields.get("labels") or issue.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    label_line = ", ".join(str(x) for x in labels) if labels else ""
    parts = [f"# {key}: {summary}", ""]
    if label_line:
        parts.append(f"Labels: {label_line}")
        parts.append("")
    parts.append(str(description).strip())
    parts.append("")
    return "\n".join(parts)


def _adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        bits = [_adf_to_text(c) for c in node.get("content") or []]
        return "\n".join(b for b in bits if b)
    if isinstance(node, list):
        return "\n".join(_adf_to_text(x) for x in node)
    return ""


def load_json_export(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    issues: list[dict[str, Any]]
    if isinstance(data, list):
        issues = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict) and isinstance(data.get("issues"), list):
        issues = [x for x in data["issues"] if isinstance(x, dict)]
    elif isinstance(data, dict):
        issues = [data]
    else:
        raise ValueError("JSON export must be an issue object, list, or {issues: [...]}")
    return [_issue_to_markdown(i) for i in issues]


def fetch_issue_rest(key: str) -> str:
    base = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
    _maybe_refresh_oauth()
    oauth = (
        os.environ.get("JIRA_OAUTH_ACCESS_TOKEN")
        or os.environ.get("JIRA_ACCESS_TOKEN")
        or ""
    ).strip()
    token = oauth or (os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_TOKEN") or "")
    user = os.environ.get("JIRA_USER") or os.environ.get("JIRA_EMAIL") or ""
    if not base or not token:
        raise RuntimeError(
            "JIRA_BASE_URL and JIRA_OAUTH_ACCESS_TOKEN (or JIRA_API_TOKEN) required for live Jira fetch"
        )
    url = f"{base}/rest/api/2/issue/{urllib.parse.quote(key)}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if oauth or not user:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        import base64

        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — operator-configured URL
        payload = json.loads(resp.read().decode("utf-8"))
    return _issue_to_markdown(payload)


def _maybe_refresh_oauth() -> None:
    """If JIRA_OAUTH_REFRESH_TOKEN + client credentials are set, refresh access token.

    Writes the new access token into the process env for subsequent calls.
    No-op when refresh credentials are absent.
    """
    refresh = (os.environ.get("JIRA_OAUTH_REFRESH_TOKEN") or "").strip()
    client_id = (os.environ.get("JIRA_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("JIRA_OAUTH_CLIENT_SECRET") or "").strip()
    token_url = (
        os.environ.get("JIRA_OAUTH_TOKEN_URL")
        or "https://auth.atlassian.com/oauth/token"
    ).strip()
    if not (refresh and client_id and client_secret):
        return
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
        }
    ).encode()
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return
    access = str(payload.get("access_token") or "").strip()
    if access:
        os.environ["JIRA_OAUTH_ACCESS_TOKEN"] = access
    new_refresh = str(payload.get("refresh_token") or "").strip()
    if new_refresh:
        os.environ["JIRA_OAUTH_REFRESH_TOKEN"] = new_refresh


def load(
    *,
    issue_keys: list[str] | None = None,
    export_paths: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Returns (spec_contents, labels_for_paths, errors)."""
    contents: list[str] = []
    labels: list[str] = []
    errors: list[str] = []
    for raw in export_paths or []:
        p = Path(raw)
        if not p.exists():
            errors.append(f"{raw}: not found")
            continue
        try:
            specs = load_json_export(p)
            contents.extend(specs)
            labels.extend([f"{p}#{i}" for i in range(len(specs))])
        except Exception as exc:
            errors.append(f"{raw}: {exc}")
    for key in issue_keys or []:
        key = key.strip()
        if not key:
            continue
        try:
            contents.append(fetch_issue_rest(key))
            labels.append(f"jira:{key}")
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            errors.append(f"{key}: {exc}")
    return contents, labels, errors
