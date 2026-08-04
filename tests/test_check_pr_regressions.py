import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import check_pr_regressions as cpr  # noqa: E402


def test_normalize_check_name_strips_trigger_context():
    assert cpr.normalize_check_name("test (pull_request)") == "test"
    assert cpr.normalize_check_name("test (push)") == "test"
    assert cpr.normalize_check_name("CI / test (pull_request)") == "ci / test"
    assert cpr.normalize_check_name("CI / test (push)") == "ci / test"
    assert cpr.normalize_check_name("build-pull_request") == "build"
    assert cpr.normalize_check_name("build-push") == "build"


def test_normalize_check_name_leaves_unrelated_names_alone():
    assert cpr.normalize_check_name("lint") == "lint"
    assert cpr.normalize_check_name("Deploy Preview") == "deploy preview"


def test_latest_by_name_keeps_most_recent_rerun():
    runs = [
        {"name": "test (push)", "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T00:00:00Z"},
        {"name": "test (push)", "status": "completed", "conclusion": "success", "started_at": "2026-01-01T01:00:00Z"},
    ]
    latest = cpr.latest_by_name(runs)
    assert latest["test"]["conclusion"] == "success"


def test_diff_checks_approve_when_pr_matches_default_under_different_names():
    default_runs = [{"name": "test (push)", "status": "completed", "conclusion": "success", "started_at": "t1"}]
    pr_runs = [{"name": "test (pull_request)", "status": "completed", "conclusion": "success", "started_at": "t1"}]
    result = cpr.diff_checks(default_runs, pr_runs)
    assert result["status"] == "approve"
    assert result["regressed"] == []
    assert result["missing"] == []


def test_diff_checks_flags_regression_on_new_failure():
    default_runs = [{"name": "test (push)", "status": "completed", "conclusion": "success", "started_at": "t1"}]
    pr_runs = [{"name": "test (pull_request)", "status": "completed", "conclusion": "failure", "started_at": "t1"}]
    result = cpr.diff_checks(default_runs, pr_runs)
    assert result["status"] == "regressed"
    assert result["regressed"] == ["test"]


def test_diff_checks_flags_regression_on_missing_check():
    default_runs = [{"name": "test (push)", "status": "completed", "conclusion": "success", "started_at": "t1"}]
    pr_runs = []
    result = cpr.diff_checks(default_runs, pr_runs)
    assert result["status"] == "regressed"
    assert result["missing"] == ["test"]


def test_diff_checks_ignores_checks_already_failing_on_default():
    default_runs = [{"name": "flaky (push)", "status": "completed", "conclusion": "failure", "started_at": "t1"}]
    pr_runs = []
    result = cpr.diff_checks(default_runs, pr_runs)
    assert result["status"] == "approve"


def test_diff_checks_pending_when_pr_check_still_running():
    default_runs = [{"name": "test (push)", "status": "completed", "conclusion": "success", "started_at": "t1"}]
    pr_runs = [{"name": "test (pull_request)", "status": "in_progress", "conclusion": None, "started_at": "t1"}]
    result = cpr.diff_checks(default_runs, pr_runs)
    assert result["status"] == "pending"
    assert result["pending"] == ["test"]
