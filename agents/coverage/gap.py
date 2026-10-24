# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Compare discovered coverage inventory against existing Playwright tests."""

from __future__ import annotations

import re
from pathlib import Path

from agents.common.models import CoverageCandidate, CoverageGap, Requirement, RequirementStep


def _repo_root() -> Path:
    from orchestrator.paths import repo_root

    return repo_root()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def _collect_test_files() -> list[Path]:
    root = _repo_root()
    files: list[Path] = []
    for pattern in ("tests/manual/**/*.spec.ts", "tests/generated/**/*.spec.ts"):
        files.extend(root.glob(pattern))
    return files


def _extract_test_signals(content: str) -> set[str]:
    signals: set[str] = set()

    for match in re.finditer(r"goto\(\s*['\"`]([^'\"`]+)['\"`]", content):
        signals.add(match.group(1).lower())

    for match in re.finditer(r"toHaveURL\(\s*[^)]*['\"`]([^'\"`]+)['\"`]", content):
        signals.add(match.group(1).lower())

    for match in re.finditer(r"test(?:\.(?:only|skip))?\(\s*['\"`]([^'\"`]+)['\"`]", content):
        signals.add(match.group(1).lower())

    for match in re.finditer(r"['\"`](/[^'\"`]+)['\"`]", content):
        signals.add(match.group(1).lower())

    return signals


def is_candidate_covered(candidate: CoverageCandidate, test_signals: set[str]) -> bool:
    path = candidate.path.lower().rstrip("/") or "/"
    if path in test_signals:
        return True
    if path != "/" and any(path in signal for signal in test_signals):
        return True
    slug = _slug(candidate.path.strip("/"))
    if slug and any(slug in signal for signal in test_signals):
        return True
    return False


def _is_routable_path(path: str) -> bool:
    """Return False for slugified markdown headings that are not real URLs."""
    normalized = path.strip("/")
    if not normalized:
        return True
    if re.match(r"^\d+-", normalized):
        return False
    if re.match(r"^\d+$", normalized):
        return False
    return True


def _infer_route_path(candidate: CoverageCandidate) -> str | None:
    if candidate.kind in {"route", "page", "api"}:
        path = candidate.path if candidate.path.startswith("/") else f"/{candidate.path.lstrip('/')}"
        return path if _is_routable_path(path) else None

    if candidate.kind == "doc":
        if candidate.source_file and "src/pages/" in candidate.source_file.replace("\\", "/"):
            return _infer_route_path(
                CoverageCandidate(
                    id=candidate.id,
                    kind="route",
                    path=candidate.source_file,
                    title=candidate.title,
                )
            )
        return None

    return None


def _assertion_text(candidate: CoverageCandidate) -> str:
    title = candidate.title.strip()
    if re.match(r"^\d+\.?\s", title):
        title = re.sub(r"^\d+\.?\s*", "", title)
    if candidate.context:
        context = re.sub(r"^#+\s*", "", candidate.context).strip()
        if context and context != title and not re.search(
            r"(^//|/\*|import |export |function |const |=>|\{|\})",
            context,
        ):
            if len(context) <= 60 and context.count("/") < 2:
                return context
    return title[:60] if title else "content"


def analyze_gaps(candidates: list[CoverageCandidate]) -> tuple[list[CoverageGap], list[CoverageCandidate]]:
    """Return uncovered gaps and covered candidates."""
    test_signals: set[str] = set()
    for path in _collect_test_files():
        try:
            test_signals.update(_extract_test_signals(path.read_text(encoding="utf-8")))
        except Exception:
            continue

    gaps: list[CoverageGap] = []
    covered: list[CoverageCandidate] = []
    for candidate in candidates:
        if is_candidate_covered(candidate, test_signals):
            covered.append(candidate)
        else:
            route_path = _infer_route_path(candidate)
            if route_path is None:
                continue
            gaps.append(CoverageGap(candidate=candidate))

    priority_order = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda g: (priority_order.get(g.candidate.priority, 9), g.candidate.path))
    return gaps, covered


def gaps_to_requirements(gaps: list[CoverageGap]) -> list[Requirement]:
    """Convert coverage gaps into structured test requirements."""
    requirements: list[Requirement] = []
    for gap in gaps:
        candidate = gap.candidate
        path = _infer_route_path(candidate)
        if not path:
            continue

        assertion = _assertion_text(candidate)
        steps = [
            RequirementStep(action="navigate", target=path),
            RequirementStep(action="wait"),
            RequirementStep(
                action="assert",
                target="heading",
                assertion=assertion,
            ),
        ]
        tags = ["coverage", candidate.kind, f"path:{path}"]
        if candidate.source_file:
            tags.append("discovered")

        requirements.append(
            Requirement(
                id=f"coverage-{_slug(candidate.id)}",
                title=f"Coverage: {candidate.title}",
                description=(
                    f"Verify {candidate.kind} '{candidate.title}' at {path}. "
                    f"Assert visible: {assertion}. "
                    f"{candidate.context or ''}"
                ).strip(),
                priority=candidate.priority,
                steps=steps,
                tags=tags,
            )
        )
    return requirements
