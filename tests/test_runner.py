import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import actions  # noqa: E402
import github_client  # noqa: E402
import runner  # noqa: E402


REPOS = [
    {"owner": "acme", "repo": "repo-a", "default_branch": "main"},
    {"owner": "acme", "repo": "repo-b", "default_branch": "main"},
    {"owner": "acme", "repo": "repo-c", "default_branch": "master"},
]


def test_render_action_context_regex_replace_substitutes_default_branch():
    action_cfg = {
        "type": "regex_replace",
        "path": ".pre-commit-config.yaml",
        "pattern": "autoupdate_branch: develop",
        "replacement": "autoupdate_branch: {{ default_branch }}",
    }
    rendered = runner.render_action_context(action_cfg, "acme", "svc", "master", "DEVOPS-1234")
    assert rendered["replacement"] == "autoupdate_branch: master"
    # original untouched
    assert action_cfg["replacement"] == "autoupdate_branch: {{ default_branch }}"


def test_render_action_context_leaves_non_templatable_fields_alone():
    action_cfg = {
        "type": "regex_replace",
        "path": ".pre-commit-config.yaml",
        "pattern": "{{ not_a_real_placeholder }}",
        "replacement": "static text",
    }
    rendered = runner.render_action_context(action_cfg, "o", "r", "main", "b")
    # pattern isn't in TEMPLATABLE_ACTION_FIELDS for regex_replace -- left as-is
    assert rendered["pattern"] == "{{ not_a_real_placeholder }}"


def test_render_action_context_anchor_insert_lines():
    action_cfg = {
        "type": "anchor_insert",
        "path": "x.yml",
        "anchor_pattern": "steps:",
        "lines": ["  - run: echo {{ repo }} on {{ default_branch }}"],
    }
    rendered = runner.render_action_context(action_cfg, "acme", "svc-a", "main", "b")
    assert rendered["lines"] == ["  - run: echo svc-a on main"]


def test_render_action_context_add_file_is_a_noop():
    action_cfg = {"type": "add_file", "path": "x", "source_template_path": "t"}
    rendered = runner.render_action_context(action_cfg, "o", "r", "main", "b")
    assert rendered is action_cfg


def test_backreference_survives_jinja_rendering_then_regex_subn():
    # the templated replacement contains both a Jinja placeholder and a regex
    # backreference (\1) -- Jinja must render {{ }} without touching \1, and
    # re.subn must still resolve \1 afterwards.
    action_cfg = {
        "type": "regex_replace",
        "path": ".pre-commit-config.yaml",
        "pattern": r"autoupdate_branch:\s*develop(\s*\n\s*)autoupdate_schedule:\s*monthly",
        "replacement": r"autoupdate_branch: {{ default_branch }}\1autoupdate_schedule: quarterly",
    }
    rendered = runner.render_action_context(action_cfg, "acme", "svc", "master", "DEVOPS-1234")
    content = "ci:\n  autoupdate_branch: develop\n  autoupdate_schedule: monthly\n"
    result = actions.regex_replace(content, rendered["pattern"], rendered["replacement"])
    assert result == "ci:\n  autoupdate_branch: master\n  autoupdate_schedule: quarterly\n"


def test_resolve_filter_matches_content_filter(monkeypatch):
    def fake_fetch_files_batch(items, chunk_size=60):
        return {
            ("acme", "repo-a", "f.yml"): "has needle",
            ("acme", "repo-b", "f.yml"): "no match here",
            ("acme", "repo-c", "f.yml"): None,
        }

    monkeypatch.setattr(github_client, "fetch_files_batch", fake_fetch_files_batch)
    matched = runner.resolve_filter_matches(
        {"type": "content_match", "path": "f.yml", "pattern": "needle"}, REPOS,
    )
    assert matched == [("acme", "repo-a", "main")]


def test_resolve_filter_matches_pr_open_on_branch(monkeypatch):
    def fake_fetch_open_pr_branches(repo_pairs, branch, chunk_size=60):
        assert branch == "DEVOPS-1234"
        return {("acme", "repo-a"): True, ("acme", "repo-b"): False, ("acme", "repo-c"): True}

    monkeypatch.setattr(github_client, "fetch_open_pr_branches", fake_fetch_open_pr_branches)
    matched = runner.resolve_filter_matches({"type": "pr_open_on_branch", "branch": "DEVOPS-1234"}, REPOS)
    assert matched == [("acme", "repo-a", "main"), ("acme", "repo-c", "master")]


def test_resolve_filter_matches_all_of_narrows_sequentially(monkeypatch):
    # first stage (PR filter) matches repo-a and repo-b; second stage
    # (content filter) is only ever asked about those two, never repo-c --
    # proving it's sequential narrowing, not independent fetch-then-intersect.
    content_fetch_calls = []

    def fake_fetch_open_pr_branches(repo_pairs, branch, chunk_size=60):
        return {("acme", "repo-a"): True, ("acme", "repo-b"): True, ("acme", "repo-c"): False}

    def fake_fetch_files_batch(items, chunk_size=60):
        content_fetch_calls.append(items)
        return {
            ("acme", "repo-a", "f.yml"): "needle",
            ("acme", "repo-b", "f.yml"): "no match",
        }

    monkeypatch.setattr(github_client, "fetch_open_pr_branches", fake_fetch_open_pr_branches)
    monkeypatch.setattr(github_client, "fetch_files_batch", fake_fetch_files_batch)

    filter_cfg = {
        "type": "all_of",
        "filters": [
            {"type": "pr_open_on_branch", "branch": "DEVOPS-1234"},
            {"type": "content_match", "path": "f.yml", "pattern": "needle"},
        ],
    }
    matched = runner.resolve_filter_matches(filter_cfg, REPOS)
    assert matched == [("acme", "repo-a", "main")]

    # the content-match stage was only ever asked about repo-a/repo-b
    fetched_repos = {(o, r) for items in content_fetch_calls for o, r, *_ in items}
    assert fetched_repos == {("acme", "repo-a"), ("acme", "repo-b")}


def test_resolve_filter_matches_all_of_short_circuits_on_empty(monkeypatch):
    calls = []

    def fake_fetch_open_pr_branches(repo_pairs, branch, chunk_size=60):
        return {("acme", "repo-a"): False, ("acme", "repo-b"): False, ("acme", "repo-c"): False}

    def fake_fetch_files_batch(items, chunk_size=60):
        calls.append(items)
        return {}

    monkeypatch.setattr(github_client, "fetch_open_pr_branches", fake_fetch_open_pr_branches)
    monkeypatch.setattr(github_client, "fetch_files_batch", fake_fetch_files_batch)

    filter_cfg = {
        "type": "all_of",
        "filters": [
            {"type": "pr_open_on_branch", "branch": "DEVOPS-1234"},
            {"type": "content_match", "path": "f.yml", "pattern": "needle"},
        ],
    }
    matched = runner.resolve_filter_matches(filter_cfg, REPOS)
    assert matched == []
    assert calls == []  # second stage never ran -- nothing survived the first


def test_describe_filter():
    assert runner.describe_filter({"type": "content_match", "path": "f.yml", "pattern": "x"}) == "content_match(f.yml)"
    assert runner.describe_filter({"type": "pr_open_on_branch", "branch": "b"}) == "pr_open_on_branch(b)"
    assert runner.describe_filter({
        "type": "all_of",
        "filters": [
            {"type": "pr_open_on_branch", "branch": "b"},
            {"type": "content_match", "path": "f.yml", "pattern": "x"},
        ],
    }) == "pr_open_on_branch(b) AND content_match(f.yml)"
