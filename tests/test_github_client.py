"""Regression coverage for github_client's non-content-fetch helpers.

github_client never talks to a real subprocess here -- subprocess.run is
monkeypatched to return canned CompletedProcess objects, matching GitHub's
actual (stable) REST API error message text rather than assuming any
particular `gh` CLI stderr formatting.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import github_client  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_create_branch_treats_already_exists_as_non_fatal_skip(monkeypatch):
    def fake_run(cmd, input=None, capture_output=None, text=None):
        return _FakeCompletedProcess(1, stderr="gh: Reference already exists (HTTP 422)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    created = github_client.create_branch("acme", "repo-a", "DEVOPS-1234", "deadbeef")
    assert created is False


def test_create_branch_reraises_unrelated_failures(monkeypatch):
    def fake_run(cmd, input=None, capture_output=None, text=None):
        return _FakeCompletedProcess(1, stderr="gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        github_client.create_branch("acme", "repo-a", "DEVOPS-1234", "deadbeef")
        assert False, "expected GhError"
    except github_client.GhError as e:
        assert "Not Found" in str(e)


def test_create_pr_reuses_existing_pr_on_already_exists(monkeypatch):
    calls = []

    def fake_run(cmd, input=None, capture_output=None, text=None):
        calls.append(cmd)
        if "--jq" in cmd and cmd[cmd.index("--jq") + 1] == ".[0].html_url":
            # first call: no PR found yet (pre-create check).
            # second call (after "already exists"): the PR is there now.
            if len([c for c in calls if "--jq" in c]) == 1:
                return _FakeCompletedProcess(0, stdout="\n")
            return _FakeCompletedProcess(0, stdout="https://github.com/acme/repo-a/pull/7\n")
        return _FakeCompletedProcess(1, stderr="gh: A pull request already exists for acme:DEVOPS-1234. (HTTP 422)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    url = github_client.create_pr("acme", "repo-a", "title", "DEVOPS-1234", "main", "body")
    assert url == "https://github.com/acme/repo-a/pull/7"


def test_get_branch_tip_sha_returns_none_on_not_found(monkeypatch):
    def fake_run(cmd, input=None, capture_output=None, text=None):
        return _FakeCompletedProcess(1, stderr="gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = github_client.get_branch_tip_sha("acme", "repo-a", "DEVOPS-1234")
    assert sha is None


def test_fetch_open_prs_by_branch_returns_head_sha_and_none_when_absent(monkeypatch):
    def fake_run(cmd, input=None, capture_output=None, text=None):
        assert cmd[:3] == ["gh", "api", "graphql"]
        body = json.loads(input)
        variables = body["variables"]
        assert variables["branch"] == "DEVOPS-1234"
        data = {}
        i = 0
        while f"o{i}" in variables:
            repo = variables[f"n{i}"]
            if repo == "repo-a":
                data[f"r{i}"] = {"pullRequests": {"nodes": [
                    {"number": 7, "url": "https://github.com/acme/repo-a/pull/7", "headRefOid": "deadbeef"},
                ]}}
            else:
                data[f"r{i}"] = {"pullRequests": {"nodes": []}}
            i += 1
        return _FakeCompletedProcess(0, stdout=json.dumps({"data": data}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = github_client.fetch_open_prs_by_branch(
        [("acme", "repo-a"), ("acme", "repo-b")], "DEVOPS-1234",
    )
    assert result[("acme", "repo-a")] == {
        "number": 7, "url": "https://github.com/acme/repo-a/pull/7", "head_sha": "deadbeef",
    }
    assert result[("acme", "repo-b")] is None


def test_get_check_runs_paginates_until_short_page(monkeypatch):
    calls = []

    def fake_run(cmd, input=None, capture_output=None, text=None):
        calls.append(cmd)
        page_value = next(a.split("=")[1] for a in cmd if a.startswith("page="))
        if page_value == "1":
            runs = [{"name": f"job-{i}", "status": "completed", "conclusion": "success", "started_at": "t"}
                    for i in range(100)]
        else:
            runs = [{"name": "job-100", "status": "completed", "conclusion": "success", "started_at": "t"}]
        stdout = "\n".join(json.dumps(r) for r in runs)
        return _FakeCompletedProcess(0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runs = github_client.get_check_runs("acme", "repo-a", "deadbeef")
    assert len(runs) == 101
    assert [c for c in calls if any(a.startswith("page=") for a in c)]


def test_fetch_open_pr_branches_batches_and_parses_totalcount(monkeypatch):
    # simulate: repo-a has an open PR on the branch, repo-b doesn't, repo-c
    # doesn't exist (repository resolves to null).
    def fake_run(cmd, input=None, capture_output=None, text=None):
        assert cmd[:3] == ["gh", "api", "graphql"]
        body = json.loads(input)
        variables = body["variables"]
        assert variables["branch"] == "DEVOPS-1234"
        data = {}
        i = 0
        while f"o{i}" in variables:
            repo = variables[f"n{i}"]
            if repo == "repo-a":
                data[f"r{i}"] = {"pullRequests": {"totalCount": 1}}
            elif repo == "repo-b":
                data[f"r{i}"] = {"pullRequests": {"totalCount": 0}}
            else:
                data[f"r{i}"] = None
            i += 1
        return _FakeCompletedProcess(0, stdout=json.dumps({"data": data}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = github_client.fetch_open_pr_branches(
        [("acme", "repo-a"), ("acme", "repo-b"), ("acme", "repo-c")], "DEVOPS-1234",
    )
    assert result == {
        ("acme", "repo-a"): True,
        ("acme", "repo-b"): False,
        ("acme", "repo-c"): False,
    }
