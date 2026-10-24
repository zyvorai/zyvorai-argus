# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Remember autofix patches that were applied and confirmed passing."""

from __future__ import annotations

import os

from agents.common.models import AutofixSuggestion
from agents.skills.store import load_skills, record_confirmed_fix, save_skills
from orchestrator.state import PipelineState


def confirmed_fixes(state: PipelineState) -> list[AutofixSuggestion]:
    """Autofix suggestions that were written to a spec AND whose retry passed.

    Shared by the skill store and heal-PRs so "confirmed" has one definition:
    a patched file exists, the re-run passed, and the suggestion says it was
    applied. Empty when nothing is confirmed.
    """
    metadata = state.get("metadata", {})
    test_results = state.get("test_results")

    if not metadata.get("autofix_patched_files"):
        return []
    if not test_results or not test_results.all_passed:
        return []
    return [s for s in state.get("autofix_suggestions", []) if "[applied to" in s.explanation]


def learn_skills_node(state: PipelineState) -> PipelineState:
    """Persist applied autofix suggestions as skills once a retry passes."""
    confirmed = confirmed_fixes(state)
    if not confirmed:
        return state

    run_id = os.environ.get("GITHUB_RUN_ID")
    skills = load_skills()
    for suggestion in confirmed:
        skills = record_confirmed_fix(skills, suggestion, run_id=run_id)
    save_skills(skills)

    return state
