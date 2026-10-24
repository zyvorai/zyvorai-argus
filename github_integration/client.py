# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""GitHub REST client for specs and PR comments."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

from github import Github, GithubException
from github.PullRequest import PullRequest


SPEC_LABELS = {"qa", "user-story", "feature-spec", "enhancement"}

DEFAULT_DISCOVERY_PATHS = [
    "docs/",
    "docs/specs/",
    "CHANGELOG.md",
    "README.md",
    "src/pages/",
    "src/routes/",
    "app/",
]

DISCOVERY_EXTENSIONS = {
    ".md",
    ".tsx",
    ".ts",
    ".jsx",
    ".js",
    ".json",
    ".yaml",
    ".yml",
}

DISCOVERY_FILENAME_PATTERNS = [
    "sidebars*.js",
    "sidebars*.ts",
    "docusaurus.config.*",
    "openapi*.json",
    "openapi*.yaml",
    "openapi*.yml",
]

DEFAULT_MAX_DISCOVERY_FILES = 200
DEFAULT_MAX_DISCOVERY_BYTES = 2_000_000


def get_discovery_paths() -> list[str]:
    """Return discovery roots from env or defaults."""
    raw = os.environ.get("COVERAGE_DISCOVERY_PATHS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return list(DEFAULT_DISCOVERY_PATHS)


def get_max_discovery_files() -> int:
    return int(os.environ.get("COVERAGE_MAX_DISCOVERY_FILES", DEFAULT_MAX_DISCOVERY_FILES))


def get_max_discovery_bytes() -> int:
    return int(os.environ.get("COVERAGE_MAX_DISCOVERY_BYTES", DEFAULT_MAX_DISCOVERY_BYTES))


def normalize_github_spec_path(spec: str) -> str:
    """Convert a GitHub blob/raw URL or repo path to a repo-relative file path."""
    spec = spec.strip()
    if spec.startswith("https://github.com/") and "/blob/" in spec:
        _, after_blob = spec.split("/blob/", 1)
        if "/" in after_blob:
            return after_blob.split("/", 1)[1]
    if spec.startswith("https://raw.githubusercontent.com/"):
        parts = spec.split("/")
        if len(parts) > 4:
            return "/".join(parts[4:])
    return spec.lstrip("/")


def resolve_github_token(explicit: str | None = None) -> str:
    """Resolve GitHub token: explicit arg → GITHUB_TOKEN env → gh CLI."""
    if explicit:
        return explicit.strip()

    env_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = resolve_github_token(token)
        self._gh = Github(self.token) if self.token else None

    @property
    def available(self) -> bool:
        return self._gh is not None

    def _repo(self, full_name: str):
        if not self._gh:
            raise RuntimeError(
                "GitHub token required. Set GITHUB_TOKEN or run `gh auth login`."
            )
        return self._gh.get_repo(full_name)

    def fetch_labeled_issues(self, repo_full_name: str, labels: set[str] | None = None) -> list[dict]:
        """Fetch open issues with QA-related labels."""
        labels = labels or SPEC_LABELS
        repo = self._repo(repo_full_name)
        results = []
        for issue in repo.get_issues(state="open"):
            issue_labels = {lbl.name.lower() for lbl in issue.labels}
            if issue_labels & labels:
                results.append(
                    {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body or "",
                        "labels": list(issue_labels),
                        "url": issue.html_url,
                    }
                )
        return results

    def fetch_pr_body(self, repo_full_name: str, pr_number: int) -> str:
        """Fetch PR description body."""
        repo = self._repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)
        return pr.body or ""

    def fetch_pr_changed_files(self, repo_full_name: str, pr_number: int) -> list[str]:
        """Return repo-relative paths changed in a pull request."""
        repo = self._repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)
        return [f.filename for f in pr.get_files()]

    def _matches_discovery_file(self, repo_path: str) -> bool:
        """Check whether a repo path is relevant for coverage discovery."""
        lower = repo_path.lower()
        if any(lower.endswith(ext) for ext in DISCOVERY_EXTENSIONS):
            return True
        basename = Path(repo_path).name
        return any(fnmatch.fnmatch(basename, pattern) for pattern in DISCOVERY_FILENAME_PATTERNS)

    def list_tree(
        self,
        repo_full_name: str,
        roots: list[str] | None = None,
        extensions: set[str] | None = None,
        max_files: int | None = None,
    ) -> list[str]:
        """Recursively list repo-relative file paths under discovery roots."""
        roots = roots or get_discovery_paths()
        extensions = extensions or DISCOVERY_EXTENSIONS
        max_files = max_files or get_max_discovery_files()
        repo = self._repo(repo_full_name)
        discovered: list[str] = []

        def walk(path: str) -> None:
            if len(discovered) >= max_files:
                return
            try:
                contents = repo.get_contents(path)
            except Exception:
                return

            if not isinstance(contents, list):
                contents = [contents]

            for item in contents:
                if len(discovered) >= max_files:
                    return
                if item.type == "dir":
                    walk(item.path)
                elif item.type == "file":
                    lower = item.path.lower()
                    if lower.endswith(tuple(extensions)) or self._matches_discovery_file(item.path):
                        discovered.append(item.path)

        for root in roots:
            if len(discovered) >= max_files:
                break
            normalized = root.rstrip("/")
            try:
                if root.endswith("/"):
                    walk(root)
                else:
                    repo.get_contents(normalized)
                    if self._matches_discovery_file(normalized) or normalized.lower().endswith(
                        tuple(extensions)
                    ):
                        discovered.append(normalized)
                    else:
                        walk(normalized + "/")
            except Exception:
                continue

        return discovered

    def fetch_paths(
        self,
        repo_full_name: str,
        repo_paths: list[str],
        max_bytes: int | None = None,
    ) -> dict[str, str]:
        """Batch-fetch file contents keyed by repo-relative path."""
        max_bytes = max_bytes or get_max_discovery_bytes()
        contents: dict[str, str] = {}
        total_bytes = 0

        for repo_path in repo_paths:
            if total_bytes >= max_bytes:
                break
            try:
                content = self.fetch_file_content(repo_full_name, repo_path)
            except Exception:
                continue
            size = len(content.encode("utf-8"))
            if total_bytes + size > max_bytes:
                break
            contents[repo_path] = content
            total_bytes += size

        return contents

    def download_discovery_files_to_local(
        self,
        repo_full_name: str,
        output_dir: str | Path,
        roots: list[str] | None = None,
        scope_paths: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Download discovery files locally.

        Returns (local_paths, repo_paths).
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if scope_paths:
            repo_paths = [
                normalize_github_spec_path(p)
                for p in scope_paths
                if self._matches_discovery_file(p)
                or p.lower().endswith(tuple(DISCOVERY_EXTENSIONS))
            ]
            repo_paths = repo_paths[: get_max_discovery_files()]
        else:
            repo_paths = self.list_tree(repo_full_name, roots=roots)

        local_paths: list[str] = []
        saved_repo_paths: list[str] = []

        for repo_path in repo_paths:
            try:
                content = self.fetch_file_content(repo_full_name, repo_path)
            except Exception:
                continue
            safe_name = repo_path.replace("/", "__")
            local_path = output_dir / safe_name
            local_path.write_text(content, encoding="utf-8")
            local_paths.append(str(local_path))
            saved_repo_paths.append(repo_path)

        return local_paths, saved_repo_paths

    def fetch_spec_files(self, repo_full_name: str, paths: list[str] | None = None) -> list[str]:
        """Fetch markdown spec files from the product repo."""
        paths = paths or ["docs/specs/", "CHANGELOG.md", "README.md"]
        repo = self._repo(repo_full_name)
        contents: list[str] = []

        for path in paths:
            try:
                if path.endswith("/"):
                    for item in repo.get_contents(path):
                        if item.path.endswith(".md"):
                            file_content = repo.get_contents(item.path)
                            contents.append(file_content.decoded_content.decode("utf-8"))
                else:
                    file_content = repo.get_contents(path)
                    contents.append(file_content.decoded_content.decode("utf-8"))
            except Exception:
                continue

        return contents

    def fetch_file_content(self, repo_full_name: str, repo_path: str) -> str:
        """Fetch a single file from the repository."""
        repo = self._repo(repo_full_name)
        file_content = repo.get_contents(normalize_github_spec_path(repo_path))
        return file_content.decoded_content.decode("utf-8")

    def download_files_to_local(
        self,
        repo_full_name: str,
        repo_paths: list[str],
        output_dir: str | Path,
    ) -> list[str]:
        """Download specific repo files and save locally. Returns local file paths."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []

        for repo_path in repo_paths:
            normalized = normalize_github_spec_path(repo_path)
            content = self.fetch_file_content(repo_full_name, normalized)
            safe_name = normalized.replace("/", "__")
            path = output_dir / safe_name
            path.write_text(content, encoding="utf-8")
            paths.append(str(path))

        return paths

    def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> None:
        """Post a comment on a pull request."""
        repo = self._repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)
        pr.create_issue_comment(body)

    def create_draft_pr(
        self,
        repo_full_name: str,
        *,
        branch: str,
        files: dict[str, str],
        title: str,
        body: str,
        commit_message: str,
        base: str | None = None,
    ) -> dict:
        """Open a **draft** PR that changes existing repo files, via the REST API.

        Nothing is checked out or pushed from the local clone, so the working
        tree is never touched. Only files that already exist on the base branch
        and whose content really differs are committed; the rest are reported in
        ``skipped``. Raises ``RuntimeError`` when nothing would change.
        """
        repo = self._repo(repo_full_name)
        base_branch = base or repo.default_branch
        base_sha = repo.get_branch(base_branch).commit.sha

        changes: list[tuple[str, str, str]] = []  # (path, blob sha on base, new text)
        skipped: dict[str, str] = {}
        for path, text in files.items():
            try:
                current = repo.get_contents(path, ref=base_branch)
            except GithubException as exc:
                skipped[path] = "not in the repository" if exc.status == 404 else f"HTTP {exc.status}"
                continue
            if isinstance(current, list):
                skipped[path] = "is a directory"
            elif current.decoded_content.decode("utf-8", "replace") == text:
                skipped[path] = "already matches the base branch"
            else:
                changes.append((path, current.sha, text))
        if not changes:
            raise RuntimeError(f"no file would change: {skipped}")

        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
        for path, blob_sha, text in changes:
            repo.update_file(path, commit_message, text, blob_sha, branch=branch)
        pr = repo.create_pull(title=title, body=body, head=branch, base=base_branch, draft=True)
        return {"url": pr.html_url, "number": pr.number, "changed": [c[0] for c in changes], "skipped": skipped}

    def get_pr_head_sha(self, repo_full_name: str, pr_number: int) -> str:
        """The PR's current head commit SHA — for setting a commit status."""
        repo = self._repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)
        return pr.head.sha

    def create_pr_review(self, repo_full_name: str, pr_number: int, *, event: str, body: str) -> None:
        """Post a formal PR review — `event` is APPROVE / REQUEST_CHANGES / COMMENT.

        Used by the `pr-gate` CLI command to actually block a merge (a plain
        issue comment, `post_pr_comment` above, does not)."""
        repo = self._repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)
        pr.create_review(event=event, body=body)

    def set_commit_status(
        self,
        repo_full_name: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str,
        target_url: str | None = None,
    ) -> None:
        """Set a commit status — `state` is one of error/failure/pending/success."""
        repo = self._repo(repo_full_name)
        commit = repo.get_commit(sha)
        commit.create_status(
            state=state, context=context, description=description[:140], target_url=target_url or ""
        )

    def download_spec_to_local(
        self,
        repo_full_name: str,
        output_dir: str | Path,
    ) -> list[str]:
        """Download spec content and save locally. Returns local file paths."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        issues = self.fetch_labeled_issues(repo_full_name)
        paths: list[str] = []

        for issue in issues:
            filename = f"issue-{issue['number']}.md"
            path = output_dir / filename
            content = f"# {issue['title']}\n\n{issue['body']}"
            path.write_text(content, encoding="utf-8")
            paths.append(str(path))

        for i, spec_content in enumerate(self.fetch_spec_files(repo_full_name)):
            path = output_dir / f"spec-{i}.md"
            path.write_text(spec_content, encoding="utf-8")
            paths.append(str(path))

        return paths
