# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Pluggable requirement sources beyond github/local/document.

Each connector returns a list of plain-text specs for parse_requirements.
Live SaaS connectors use env credentials; file modes work offline for CI.
"""

from __future__ import annotations

from agents.requirements_sources import email as email_src
from agents.requirements_sources import jira as jira_src
from agents.requirements_sources import transcript as transcript_src

__all__ = ["email_src", "jira_src", "transcript_src"]
