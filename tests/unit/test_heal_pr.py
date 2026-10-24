# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
from __future__ import annotations

import json

import pytest
from github import GithubException

from agents.autofix import heal_pr as hp
from agents.common.models import AutofixSuggestion, TestResult
from github_integration.client import GitHubClient
from orchestrator.nodes import heal_pr as node_module
from orchestrator.nodes.heal_pr import heal_pr_node
from orchestrator.nodes.learn_skills import confirmed_fixes


def _suggestion(**kw) -> AutofixSuggestion:
    base = dict(
        test_title="login button click",
        original_selector="#old-login-btn",
        suggested_selector="page.getByRole('button', { name: 'Log in' })",
        confidence="high",
        explanation="role-based locator [applied to login.spec.ts]",
    )
    return AutofixSuggestion(**{**base, **kw})


def _state(files, passed=True, suggestions=None):
    return {
        "metadata": {"autofix_patched_files": files},
        "test_results": TestResult(passed=1 if passed else 0, failed=0 if passed else 1, total=1, cases=[]),
        "autofix_suggestions": suggestions if suggestions is not None else [_suggestion()],
    }


# -- confirmed_fixes ------------------------------------------------------------


def test_confirmed_requires_patch_pass_and_applied_marker():
    assert len(confirmed_fixes(_state(["a.spec.ts"]))) == 1
    assert confirmed_fixes(_state([])) == []
    assert confirmed_fixes(_state(["a.spec.ts"], passed=False)) == []
    unapplied = _suggestion(explanation="role-based locator")
    assert confirmed_fixes(_state(["a.spec.ts"], suggestions=[unapplied])) == []


# -- pure helpers ---------------------------------------------------------------


def test_resolve_repo_prefers_explicit_then_actions_then_origin(monkeypatch, tmp_path):
    for name in ("ARGUS_HEAL_PR_REPO", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:zyvorai/demo.git"], check=True)
    assert hp.resolve_repo(tmp_path) == "zyvorai/demo"
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/from-actions")
    assert hp.resolve_repo(tmp_path) == "acme/from-actions"
    monkeypatch.setenv("ARGUS_HEAL_PR_REPO", "acme/explicit")
    assert hp.resolve_repo(tmp_path) == "acme/explicit"


def test_collect_files_is_repo_relative_and_rejects_outside_paths(tmp_path):
    root = tmp_path / "repo"
    (root / "tests/manual").mkdir(parents=True)
    spec = root / "tests/manual/login.spec.ts"
    spec.write_text("new", encoding="utf-8")
    outside = tmp_path / "other.spec.ts"
    outside.write_text("x", encoding="utf-8")
    files, rejected = hp.collect_files([str(spec), str(spec), str(outside), str(root / "gone.ts")], root)
    assert files == {"tests/manual/login.spec.ts": "new"}
    assert sorted(rejected) == sorted([str(outside), str(root / "gone.ts")])


def test_branch_name_is_stable_per_content_and_run():
    a = hp.branch_name({"x.ts": "1"}, "42")
    assert a == hp.branch_name({"x.ts": "1"}, "42")
    assert a != hp.branch_name({"x.ts": "2"}, "42")
    assert a.startswith("argus/heal-42-") and hp.branch_name({"x.ts": "1"}, None).startswith("argus/heal-local-")


def test_body_escapes_table_cells_and_marks_draft_review():
    body = hp.build_body([_suggestion(explanation="a | b\nc", suggested_selector="x`y|z")], "99")
    assert "draft" in body and "review" in body
    assert "a \\| b c" in body and "\n\n" in body
    assert "x'y\\|z" in body
    assert "`99`" in body


def test_title_names_files():
    assert hp.build_title({"tests/a.spec.ts": ""}) == "fix(tests): repair locators in a.spec.ts"
    assert "and 1 more" in hp.build_title({"a.ts": "", "b.ts": "", "c.ts": ""})


# -- GitHubClient.create_draft_pr -----------------------------------------------


class _Content:
    def __init__(self, text, sha="blob1"):
        self.decoded_content = text.encode()
        self.sha = sha


class _Repo:
    default_branch = "main"

    def __init__(self, existing):
        self.existing = existing
        self.calls = []

    def get_branch(self, name):
        self.calls.append(("get_branch", name))
        return type("B", (), {"commit": type("C", (), {"sha": "basesha"})()})()

    def get_contents(self, path, ref=None):
        if path not in self.existing:
            raise GithubException(404, {}, {})
        return _Content(self.existing[path])

    def create_git_ref(self, ref, sha):
        self.calls.append(("create_git_ref", ref, sha))

    def update_file(self, path, message, content, sha, branch):
        self.calls.append(("update_file", path, content, sha, branch))

    def create_pull(self, **kw):
        self.calls.append(("create_pull", kw))
        return type("P", (), {"html_url": "https://github.com/o/r/pull/7", "number": 7})()


def _client(repo):
    client = GitHubClient.__new__(GitHubClient)
    client.token, client._gh = "t", object()
    client._repo = lambda name: repo
    return client


def test_create_draft_pr_commits_only_changed_existing_files():
    repo = _Repo({"tests/a.ts": "old", "tests/same.ts": "same"})
    result = _client(repo).create_draft_pr(
        "o/r", branch="argus/heal-1", title="t", body="b", commit_message="m",
        files={"tests/a.ts": "new", "tests/same.ts": "same", "tests/generated/x.ts": "gen"},
    )
    assert result["url"].endswith("/pull/7") and result["changed"] == ["tests/a.ts"]
    assert result["skipped"] == {
        "tests/same.ts": "already matches the base branch",
        "tests/generated/x.ts": "not in the repository",
    }
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["get_branch", "create_git_ref", "update_file", "create_pull"]
    assert repo.calls[1] == ("create_git_ref", "refs/heads/argus/heal-1", "basesha")
    assert repo.calls[2][1:] == ("tests/a.ts", "new", "blob1", "argus/heal-1")
    assert repo.calls[3][1]["draft"] is True and repo.calls[3][1]["base"] == "main"


def test_create_draft_pr_creates_no_branch_when_nothing_changes():
    repo = _Repo({"tests/a.ts": "same"})
    with pytest.raises(RuntimeError, match="no file would change"):
        _client(repo).create_draft_pr("o/r", branch="b", title="t", body="b", commit_message="m", files={"tests/a.ts": "same"})
    assert "create_git_ref" not in [c[0] for c in repo.calls]


# -- the node -------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.rows = []

    def audit(self, action, **kw):
        self.rows.append((action, kw))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "tests/manual").mkdir(parents=True)
    spec = root / "tests/manual/login.spec.ts"
    spec.write_text("patched", encoding="utf-8")
    store = _Store()
    import orchestrator.persistence.store as store_module

    monkeypatch.setattr(store_module, "get_store", lambda: store)
    monkeypatch.setattr(node_module, "repo_root", lambda: root)
    monkeypatch.setenv("ARGUS_HEAL_PR_REPO", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    monkeypatch.setenv("ENABLE_HEAL_PR", "true")
    created = []

    class _FakeClient:
        available = True

        def create_draft_pr(self, repo, **kw):
            created.append((repo, kw))
            return {"url": "https://github.com/o/r/pull/7", "number": 7, "changed": list(kw["files"]), "skipped": {}}

    monkeypatch.setattr(node_module, "GitHubClient", _FakeClient)
    return type("W", (), {"spec": spec, "store": store, "created": created})


def test_node_opens_pr_and_records_url(wired):
    state = heal_pr_node(_state([str(wired.spec)]))
    assert state["metadata"]["heal_pr_url"].endswith("/pull/7")
    repo, kw = wired.created[0]
    assert repo == "o/r" and kw["files"] == {"tests/manual/login.spec.ts": "patched"}
    assert kw["branch"].startswith("argus/heal-77-") and "role-based locator" in kw["body"]
    assert [r[0] for r in wired.store.rows] == ["heal_pr.create"]
    assert wired.store.rows[0][1]["outcome"] == "success"


def test_node_is_off_by_default(wired, monkeypatch):
    monkeypatch.delenv("ENABLE_HEAL_PR")
    state = heal_pr_node(_state([str(wired.spec)]))
    assert "heal_pr_url" not in state["metadata"] and wired.created == []


def test_node_does_nothing_unless_the_fix_was_confirmed(wired):
    state = heal_pr_node(_state([str(wired.spec)], passed=False))
    assert wired.created == [] and "heal_pr_error" not in state["metadata"]


def test_node_does_not_open_a_second_pr_in_the_same_run(wired):
    state = _state([str(wired.spec)])
    heal_pr_node(state)
    heal_pr_node(state)
    assert len(wired.created) == 1


def test_node_never_raises_and_records_the_error(wired, monkeypatch):
    class _Boom:
        available = True

        def create_draft_pr(self, *a, **k):
            raise RuntimeError("GitHub said no")

    monkeypatch.setattr(node_module, "GitHubClient", _Boom)
    state = heal_pr_node(_state([str(wired.spec)]))
    assert "GitHub said no" in state["metadata"]["heal_pr_error"]
    assert "heal_pr_url" not in state["metadata"]
    assert wired.store.rows[0][1]["outcome"] == "failure"


def test_node_reports_missing_token_and_never_leaks_it(wired, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SUPERSECRETTOKEN123")

    class _NoToken:
        available = False

    monkeypatch.setattr(node_module, "GitHubClient", _NoToken)
    state = heal_pr_node(_state([str(wired.spec)]))
    assert "no GitHub token" in state["metadata"]["heal_pr_error"]
    assert "SUPERSECRET" not in json.dumps(state["metadata"], default=str) + json.dumps(wired.store.rows, default=str)


def test_node_rejects_patched_files_outside_the_repo(wired, tmp_path):
    outside = tmp_path / "elsewhere.spec.ts"
    outside.write_text("x", encoding="utf-8")
    state = heal_pr_node(_state([str(outside)]))
    assert "inside the repository" in state["metadata"]["heal_pr_error"] and wired.created == []
