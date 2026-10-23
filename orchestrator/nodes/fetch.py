# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Fetch requirements from GitHub, local specs, documents, or connectors."""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.coverage_config import coverage_expansion_enabled
from orchestrator.state import PipelineState


def _repo_root() -> Path:
    from orchestrator.paths import repo_root

    return repo_root()


def fetch_requirements(state: PipelineState) -> PipelineState:
    """Fetch spec files based on source."""
    source = state.get("source", "local")
    spec_paths: list[str] = list(state.get("spec_paths", []))
    spec_contents: list[str] = []
    metadata = dict(state.get("metadata", {}))

    if source == "github":
        repo = state.get("repo_full_name") or os.environ.get("ZYVOR_PRODUCT_REPO", "")
        if not repo:
            return {**state, "error": "ZYVOR_PRODUCT_REPO is not set"}

        from github_integration.client import GitHubClient

        client = GitHubClient()
        if not client.available:
            return {**state, "error": "GitHub token required. Set GITHUB_TOKEN or run `gh auth login`."}

        output_dir = _repo_root() / "tests" / "fixtures" / "fetched"
        requested = [p for p in spec_paths if not Path(p).exists()]
        try:
            if requested:
                spec_paths = client.download_files_to_local(repo, requested, output_dir)
            else:
                spec_paths = client.download_spec_to_local(repo, output_dir)
        except Exception as exc:
            return {**state, "error": f"Failed to fetch spec from GitHub: {exc}"}

        pr_number = state.get("pr_number")
        if pr_number:
            try:
                pr_body = client.fetch_pr_body(repo, pr_number)
                if pr_body.strip():
                    pr_path = output_dir / f"pr-{pr_number}-body.md"
                    pr_path.write_text(f"# PR #{pr_number}\n\n{pr_body}", encoding="utf-8")
                    spec_paths.append(str(pr_path))
                    metadata["pr_body_fetched"] = True
            except Exception:
                pass

            if not metadata.get("changed_files"):
                try:
                    metadata["changed_files"] = client.fetch_pr_changed_files(repo, pr_number)
                except Exception:
                    metadata["changed_files"] = []

        if coverage_expansion_enabled(state):
            code_dir = output_dir / "code"
            scope_paths = metadata.get("changed_files") or None
            try:
                local_paths, repo_paths = client.download_discovery_files_to_local(
                    repo,
                    code_dir,
                    scope_paths=scope_paths,
                )
                metadata["discovered_paths"] = repo_paths
                metadata["discovered_local_paths"] = local_paths
            except Exception as exc:
                metadata["discovery_error"] = str(exc)

    if source == "document":
        document_paths = list(state.get("document_paths", []))
        if not document_paths:
            return {**state, "error": "document source requires at least one document_paths entry"}

        from knowledge.documents import SUPPORTED_SUFFIXES, read_text

        for doc_path in document_paths:
            p = Path(doc_path)
            if not p.exists():
                metadata.setdefault("document_errors", []).append(f"{doc_path}: not found")
                continue
            if p.suffix.lower() not in SUPPORTED_SUFFIXES:
                metadata.setdefault("document_errors", []).append(f"{doc_path}: unsupported file type")
                continue
            try:
                spec_contents.append(read_text(p))
                spec_paths.append(str(p))
            except Exception as exc:
                metadata.setdefault("document_errors", []).append(f"{doc_path}: {exc}")

        return {
            **state,
            "spec_paths": spec_paths,
            "spec_contents": spec_contents,
            "metadata": metadata,
        }

    if source == "email":
        from agents.requirements_sources.email import fetch_imap, load_paths as load_email

        paths = list(state.get("document_paths") or state.get("spec_paths") or [])
        if paths:
            contents, used, errors = load_email(paths)
        else:
            contents, used, errors = fetch_imap(
                limit=int(metadata.get("imap_limit") or 20),
                subject_contains=metadata.get("imap_subject") or None,
            )
        if errors:
            metadata["email_errors"] = errors
        if not contents:
            return {
                **state,
                "error": "email source produced no specs (provide .eml paths or IMAP credentials)",
                "metadata": metadata,
            }
        return {**state, "spec_paths": used, "spec_contents": contents, "metadata": metadata}

    if source == "transcript":
        from agents.requirements_sources.transcript import load_paths as load_transcript

        paths = list(state.get("document_paths") or state.get("spec_paths") or [])
        if not paths:
            return {**state, "error": "transcript source requires paths in document_paths or spec_paths"}
        contents, used, errors = load_transcript(paths)
        if errors:
            metadata["transcript_errors"] = errors
        if not contents:
            return {**state, "error": "transcript source produced no specs", "metadata": metadata}
        return {**state, "spec_paths": used, "spec_contents": contents, "metadata": metadata}

    if source == "diarize":
        from agents.requirements_sources.diarize import load_paths as load_diarize

        paths = list(state.get("document_paths") or state.get("spec_paths") or [])
        if not paths:
            return {
                **state,
                "error": "diarize source requires audio (.wav/.mp3/…) or speaker-tagged .vtt paths",
            }
        contents, used, errors = load_diarize(paths)
        if errors:
            metadata["diarize_errors"] = errors
        if not contents:
            return {**state, "error": "diarize source produced no specs", "metadata": metadata}
        return {**state, "spec_paths": used, "spec_contents": contents, "metadata": metadata}

    if source == "jira":
        from agents.requirements_sources.jira import load as load_jira

        keys = list(state.get("jira_issue_keys") or metadata.get("jira_issue_keys") or [])
        exports = list(state.get("document_paths") or [])
        for spec_path in state.get("spec_paths") or []:
            if str(spec_path).lower().endswith(".json"):
                exports.append(str(spec_path))
        if not keys and not exports:
            return {
                **state,
                "error": "jira source requires jira_issue_keys and/or JSON export paths in document_paths",
            }
        contents, labels, errors = load_jira(issue_keys=keys, export_paths=exports)
        if errors:
            metadata["jira_errors"] = errors
        if not contents:
            return {**state, "error": "jira source produced no specs", "metadata": metadata}
        return {**state, "spec_paths": labels, "spec_contents": contents, "metadata": metadata}

    for path in spec_paths:
        p = Path(path)
        if p.exists():
            spec_contents.append(p.read_text(encoding="utf-8"))

    if not spec_paths and source == "local":
        default_spec = _repo_root() / "prompts" / "examples" / "vm-create.md"
        if default_spec.exists():
            spec_paths = [str(default_spec)]
            spec_contents = [default_spec.read_text(encoding="utf-8")]

    return {
        **state,
        "spec_paths": spec_paths,
        "spec_contents": spec_contents,
        "repo_full_name": state.get("repo_full_name") or os.environ.get("ZYVOR_PRODUCT_REPO"),
        "metadata": metadata,
    }
