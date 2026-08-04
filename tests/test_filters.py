import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import filters  # noqa: E402


def test_path_exists():
    assert filters.path_exists("some text") is True
    assert filters.path_exists("") is True
    assert filters.path_exists(None) is False


def test_path_absent():
    assert filters.path_absent(None) is True
    assert filters.path_absent("x") is False


def test_content_match():
    assert filters.content_match("uses: org/central-workflow@v2", r"org/central-workflow@v2") is True
    assert filters.content_match("uses: org/central-workflow@v3", r"org/central-workflow@v2") is False
    assert filters.content_match(None, r"anything") is False


def test_content_absent():
    assert filters.content_absent(None, r"anything") is True
    assert filters.content_absent("no match here", r"needle") is True
    assert filters.content_absent("has needle here", r"needle") is False


def test_apply_filter_content_match():
    repos = [
        {"owner": "o", "repo": "a", "default_branch": "main"},
        {"owner": "o", "repo": "b", "default_branch": "main"},
        {"owner": "o", "repo": "c", "default_branch": "master"},
    ]
    fetched = {
        ("o", "a"): "uses: org/central-workflow@v2",
        ("o", "b"): "uses: org/central-workflow@v3",
        ("o", "c"): None,
    }
    filter_cfg = {"type": "content_match", "path": ".github/workflows/ci.yml", "pattern": r"org/central-workflow@v2"}
    matched = filters.apply_filter(filter_cfg, repos, fetched)
    assert matched == [("o", "a", "main")]


def test_pr_open_on_branch():
    assert filters.pr_open_on_branch(True) is True
    assert filters.pr_open_on_branch(False) is False


def test_apply_filter_pr_open_on_branch():
    repos = [
        {"owner": "o", "repo": "a", "default_branch": "main"},
        {"owner": "o", "repo": "b", "default_branch": "main"},
        {"owner": "o", "repo": "c", "default_branch": "master"},
    ]
    fetched = {("o", "a"): True, ("o", "b"): False, ("o", "c"): True}
    filter_cfg = {"type": "pr_open_on_branch", "branch": "DEVOPS-XXXX"}
    matched = filters.apply_filter(filter_cfg, repos, fetched)
    assert matched == [("o", "a", "main"), ("o", "c", "master")]


def test_apply_filter_path_absent():
    repos = [
        {"owner": "o", "repo": "a", "default_branch": "main"},
        {"owner": "o", "repo": "b", "default_branch": "main"},
    ]
    fetched = {("o", "a"): "exists", ("o", "b"): None}
    filter_cfg = {"type": "path_absent", "path": "SECURITY.md"}
    matched = filters.apply_filter(filter_cfg, repos, fetched)
    assert matched == [("o", "b", "main")]
