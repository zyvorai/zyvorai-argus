# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Unit tests for IMAP / diarize / rule-based typed dependency connectors."""

from __future__ import annotations

from pathlib import Path

from agents.common.models import Requirement
from agents.requirement_entities.rule_fallback import extract_requirement_entities_rule_based
from agents.requirements_sources.diarize import format_speaker_lines, load_paths as load_diarize
from agents.requirements_sources.email import load_eml


def test_rule_fallback_extracts_order_depends_on_payment():
    req = Requirement(
        id="r1",
        title="Checkout",
        description="Order depends on Payment before fulfillment.",
        source_type="document",
        origin_id="specs/checkout.md",
    )
    ents = extract_requirement_entities_rule_based(req)
    assert any(d.source == "Order" and d.target == "Payment" for d in ents.model_dependencies)
    assert "Order" in ents.data_models
    assert "Payment" in ents.data_models


def test_format_speaker_lines_from_vtt_voice_tags():
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Alice>We need Order to depend on Payment\n"
    out = format_speaker_lines(raw)
    assert "Alice: We need Order to depend on Payment" in out


def test_diarize_load_speaker_vtt(tmp_path: Path):
    p = tmp_path / "meet.vtt"
    p.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Bob>Ship the feature\n",
        encoding="utf-8",
    )
    contents, used, errors = load_diarize([str(p)])
    assert not errors
    assert used == [str(p)]
    assert "Bob: Ship the feature" in contents[0]


def test_load_eml_subject_and_body(tmp_path: Path):
    p = tmp_path / "req.eml"
    p.write_text(
        "From: a@b.com\nTo: c@d.com\nSubject: Need Order flow\nMIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n\nOrder depends on Payment.\n",
        encoding="utf-8",
    )
    text = load_eml(p)
    assert "Need Order flow" in text
    assert "Order depends on Payment" in text
