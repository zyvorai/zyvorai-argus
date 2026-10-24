# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Open a draft PR for autofix repairs that a re-run confirmed.

Off unless ``ENABLE_HEAL_PR=true``. Runs after ``learn_skills`` (same
"confirmed" test, shared via ``confirmed_fixes``) and can never fail a run: any
problem is recorded in ``metadata["heal_pr_error"]`` and the pipeline carries on.
The token (``GITHUB_TOKEN`` or ``gh auth``) needs write access to contents and
pull requests; it is never logged or audited.
"""

from __future__ import annotations

import logging
import os

from agents.autofix import heal_pr
from github_integration.client import GitHubClient
from orchestrator.nodes.learn_skills import confirmed_fixes
from orchestrator.paths import repo_root
from orchestrator.state import PipelineState

log = logging.getLogger(__name__)


def _audit(outcome: str, detail: dict) -> None:
    try:
        from orchestrator.persistence.store import get_store

        get_store().audit("heal_pr.create", actor="system", resource_type="pull_request", outcome=outcome, detail=detail)
    except Exception:  # noqa: BLE001 - auditing must not break the run
        log.exception("could not audit heal_pr")


def heal_pr_node(state: PipelineState) -> PipelineState:
    if not heal_pr.enabled():
        return state
    metadata = state.setdefault("metadata", {})
    if metadata.get("heal_pr_url"):
        return state  # already opened for this run

    confirmed = confirmed_fixes(state)
    if not confirmed:
        return state

    try:
        root = repo_root()
        repo = heal_pr.resolve_repo(root)
        if not repo:
            raise RuntimeError("cannot tell which repository to open the PR in (set ARGUS_HEAL_PR_REPO)")
        files, rejected = heal_pr.collect_files(metadata.get("autofix_patched_files", []), root)
        if not files:
            raise RuntimeError(f"no patched file is inside the repository ({len(rejected)} rejected)")
        client = GitHubClient()
        if not client.available:
            raise RuntimeError("no GitHub token (set GITHUB_TOKEN or run `gh auth login`)")

        run_id = os.environ.get("GITHUB_RUN_ID")
        result = client.create_draft_pr(
            repo,
            branch=heal_pr.branch_name(files, run_id),
            files=files,
            title=heal_pr.build_title(files),
            body=heal_pr.build_body(confirmed, run_id),
            commit_message="fix(tests): repair locators confirmed by an Argus re-run",
            base=os.environ.get("ARGUS_HEAL_PR_BASE") or None,
        )
        metadata["heal_pr_url"] = result["url"]
        metadata["heal_pr_files"] = result["changed"]
        _audit("success", {"repo": repo, "pr": result["number"], "files": result["changed"], "skipped": result["skipped"]})
    except Exception as exc:  # noqa: BLE001 - a failed PR must never fail the run
        message = f"{type(exc).__name__}: {exc}"[:500]
        metadata["heal_pr_error"] = message
        log.warning("heal_pr skipped: %s", message)
        _audit("failure", {"error": message})
    return state
