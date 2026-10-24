# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

from __future__ import annotations

import email.message
from pathlib import Path

from agents.requirements_sources.email import load_eml, load_paths as load_email
from agents.requirements_sources.jira import _issue_to_markdown, load as load_jira, load_json_export
from agents.requirements_sources.transcript import load_transcript
from orchestrator.nodes.fetch import fetch_requirements


def test_email_eml_roundtrip(tmp_path: Path):
    msg = email.message.EmailMessage()
    msg["Subject"] = "Need SSO for admin console"
    msg.set_content("As an admin I want OIDC login so that auditors can sign in.")
    path = tmp_path / "sso.eml"
    path.write_bytes(msg.as_bytes())
    text = load_eml(path)
    assert "Need SSO" in text
    assert "OIDC" in text
    contents, used, errors = load_email([str(path)])
    assert not errors
    assert used == [str(path)]
    assert "OIDC" in contents[0]


def test_transcript_vtt(tmp_path: Path):
    path = tmp_path / "standup.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nWe need Order export by Friday.\n",
        encoding="utf-8",
    )
    text = load_transcript(path)
    assert "Meeting transcript" in text
    assert "Order export" in text


def test_jira_json_export(tmp_path: Path):
    path = tmp_path / "issues.json"
    path.write_text(
        '{"issues":[{"key":"ARG-1","fields":{"summary":"Port scan UX","description":"Show open ports in findings."}}]}',
        encoding="utf-8",
    )
    specs = load_json_export(path)
    assert len(specs) == 1
    assert "ARG-1" in specs[0]
    assert "Port scan UX" in specs[0]
    contents, labels, errors = load_jira(export_paths=[str(path)])
    assert not errors
    assert contents and "findings" in contents[0]


def test_jira_issue_to_markdown_flat():
    md = _issue_to_markdown({"key": "X-2", "summary": "Title", "body": "Desc", "labels": ["sec"]})
    assert "X-2: Title" in md
    assert "Labels: sec" in md


def test_fetch_email_source(tmp_path: Path):
    msg = email.message.EmailMessage()
    msg["Subject"] = "Billing export"
    msg.set_content("Export invoices as CSV.")
    path = tmp_path / "bill.eml"
    path.write_bytes(msg.as_bytes())
    state = fetch_requirements({"source": "email", "document_paths": [str(path)], "metadata": {}})
    assert not state.get("error")
    assert state["spec_contents"]
    assert "Billing export" in state["spec_contents"][0]


def test_fetch_jira_export_source(tmp_path: Path):
    path = tmp_path / "one.json"
    path.write_text('{"key":"Z-9","fields":{"summary":"Hello","description":"World"}}', encoding="utf-8")
    state = fetch_requirements({"source": "jira", "document_paths": [str(path)], "metadata": {}})
    assert not state.get("error")
    assert "Z-9" in state["spec_contents"][0]
