# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

from __future__ import annotations

from agents.common.models import ApiValidationResult, LogIssue, RegressionDiff, TestResult
from orchestrator.coverage_config import coverage_expansion_enabled
from orchestrator.graph import route_after_analyze, route_after_apply_autofix, route_on_results


def test_route_on_results_pass():
    tr = TestResult(passed=1, failed=0, total=1)
    assert route_on_results({"test_results": tr}) == "pass"


def test_route_on_results_fail_on_test():
    tr = TestResult(passed=0, failed=1, total=1)
    assert route_on_results({"test_results": tr}) == "fail"


def test_route_on_results_fail_on_api():
    tr = TestResult(
        passed=1,
        failed=0,
        total=1,
        api_validations=[ApiValidationResult(url="/x", method="GET", passed=False)],
    )
    assert route_on_results({"test_results": tr}) == "fail"


def test_route_on_results_fail_on_log():
    tr = TestResult(
        passed=1,
        failed=0,
        total=1,
        log_issues=[LogIssue(test_title="t", severity="error", message="boom", source="console")],
    )
    assert route_on_results({"test_results": tr}) == "fail"


def test_route_on_results_fail_on_regression():
    tr = TestResult(
        passed=1,
        failed=0,
        total=1,
        regression_diffs=[RegressionDiff(file="home.png", status="fail", diff_percent=5.0)],
    )
    assert route_on_results({"test_results": tr}) == "fail"


def test_route_after_analyze_respects_max_retries(monkeypatch):
    monkeypatch.setenv("ENABLE_AUTOFIX", "true")
    monkeypatch.setenv("AUTOFIX_MAX_RETRIES", "2")
    assert route_after_analyze({"metadata": {"autofix_retries": 2}}) == "report"
    assert route_after_analyze({"metadata": {"autofix_retries": 0}}) == "autofix"


def test_route_after_apply_autofix(monkeypatch):
    monkeypatch.setenv("ENABLE_AUTOFIX_APPLY", "true")
    monkeypatch.setenv("AUTOFIX_MAX_RETRIES", "2")
    assert route_after_apply_autofix({"metadata": {}}) == "report"
    assert (
        route_after_apply_autofix({"metadata": {"autofix_patches_applied": True, "autofix_retries": 0}})
        == "execute"
    )


def test_coverage_expansion_enabled_flags(monkeypatch):
    monkeypatch.delenv("ENABLE_COVERAGE_EXPANSION", raising=False)
    assert coverage_expansion_enabled({}) is False
    assert coverage_expansion_enabled({"expand_coverage": True}) is True
    assert coverage_expansion_enabled({"metadata": {"explicit_spec": True}}) is False
    monkeypatch.setenv("ENABLE_COVERAGE_EXPANSION", "true")
    assert coverage_expansion_enabled({}) is True
