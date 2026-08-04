"""All GitHub reads/writes for the toolkit, shelled out to `gh`.

No git clone, no PyGithub, no raw PAT/requests -- every call goes through
`gh` (subprocess) so it rides on the caller's existing `gh auth login`
session.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from dataclasses import dataclass, field

CHUNK_SIZE_DEFAULT = 60
MAX_RETRIES_DEFAULT = 5
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 60

RETRYABLE_STDERR_SIGNALS = (
    "api rate limit exceeded",
    "secondary rate limit",
    "rate limit",
    "abuse detection",
    "502",
    "503",
    "504",
    "timeout",
    "connection reset",
    "temporarily unavailable",
)


class GhError(Exception):
    """A `gh` invocation failed (after retries, where applicable)."""

    def __init__(self, message: str, returncode: int | None = None,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GhGraphQLError(GhError):
    """The GraphQL endpoint responded 0 but with an `errors` payload."""

    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(f"GraphQL errors: {json.dumps(errors)}")


def _is_retryable(returncode: int, stderr: str) -> bool:
    if returncode == 0:
        return False
    lowered = stderr.lower()
    return any(signal in lowered for signal in RETRYABLE_STDERR_SIGNALS)


def run_gh(args: list[str], input_data: str | None = None,
           max_retries: int = MAX_RETRIES_DEFAULT) -> subprocess.CompletedProcess:
    """Run `gh <args>`, retrying on rate-limit / transient failures.

    Non-retryable failures (bad args, 404, 422, etc.) raise immediately with
    stdout/stderr/returncode attached so callers can inspect known-shape
    errors (e.g. "ref already exists") without a GhError killing the flow.
    """
    attempt = 0
    backoff = BACKOFF_BASE_SECONDS
    last_result = None
    while attempt <= max_retries:
        result = subprocess.run(
            ["gh", *args],
            input=input_data,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result
        last_result = result
        if attempt < max_retries and _is_retryable(result.returncode, result.stderr):
            time.sleep(min(backoff, BACKOFF_MAX_SECONDS))
            backoff *= 2
            attempt += 1
            continue
        break
    raise GhError(
        f"gh {' '.join(args[:2])} failed (exit {last_result.returncode}): {last_result.stderr.strip()}",
        returncode=last_result.returncode,
        stdout=last_result.stdout,
        stderr=last_result.stderr,
    )


def gh_graphql(query: str, variables: dict | None = None) -> dict:
    """POST {query, variables} to the GraphQL endpoint via `gh api graphql --input -`.

    Building the request body ourselves (instead of -f/-F per field) lets us
    pass real nested JSON variables (needed for createCommitOnBranch's input
    object) without hand-escaping GraphQL literal syntax.
    """
    payload = json.dumps({"query": query, "variables": variables or {}})
    result = run_gh(["api", "graphql", "--input", "-"], input_data=payload)
    response = json.loads(result.stdout)
    if response.get("errors"):
        raise GhGraphQLError(response["errors"])
    return response.get("data", {})


def _rest_call(args: list[str], allow_statuses: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run a REST `gh api` call, tolerating specific non-2xx statuses.

    `allow_statuses` lets callers treat e.g. 404/422 as an expected outcome
    (repo/branch missing, ref already exists) instead of a hard error.
    """
    try:
        return run_gh(args, max_retries=MAX_RETRIES_DEFAULT)
    except GhError as e:
        if allow_statuses and e.stderr:
            for status in allow_statuses:
                if f"HTTP {status}" in e.stderr or f"({status})" in e.stderr:
                    return subprocess.CompletedProcess(args, e.returncode, e.stdout, e.stderr)
        raise


def fetch_files_batch(
    items: list[tuple[str, str, str, str]],
    chunk_size: int = CHUNK_SIZE_DEFAULT,
) -> dict[tuple[str, str, str], str | None]:
    """Batched read of (owner, repo, branch, path) -> file text (None if missing).

    Builds one aliased GraphQL query per chunk (~50-75 repos/call) instead of
    one HTTP round-trip per file.
    """
    results: dict[tuple[str, str, str], str | None] = {}
    for start in range(0, len(items), chunk_size):
        chunk = items[start:start + chunk_size]
        var_decls = []
        query_parts = []
        variables: dict[str, str] = {}
        for i, (owner, repo, branch, path) in enumerate(chunk):
            alias = f"r{i}"
            o_var, n_var, e_var = f"o{i}", f"n{i}", f"e{i}"
            var_decls.append(f"${o_var}: String!, ${n_var}: String!, ${e_var}: String!")
            variables[o_var] = owner
            variables[n_var] = repo
            variables[e_var] = f"{branch}:{path}"
            query_parts.append(
                f'{alias}: repository(owner: ${o_var}, name: ${n_var}) '
                f'{{ object(expression: ${e_var}) {{ ... on Blob {{ text }} }} }}'
            )
        query = f"query({', '.join(var_decls)}) {{ {' '.join(query_parts)} }}"
        data = gh_graphql(query, variables)
        for i, (owner, repo, branch, path) in enumerate(chunk):
            repo_data = data.get(f"r{i}")
            obj = (repo_data or {}).get("object") if repo_data else None
            text = obj.get("text") if obj else None
            results[(owner, repo, path)] = text
    return results


def fetch_single_file(owner: str, repo: str, branch: str, path: str) -> str | None:
    """Convenience wrapper around fetch_files_batch for a single file."""
    result = fetch_files_batch([(owner, repo, branch, path)])
    return result[(owner, repo, path)]


def get_branch_tip_sha(owner: str, repo: str, branch: str) -> str | None:
    """Current commit SHA that `branch` points at, or None if repo/branch is missing."""
    result = _rest_call(
        ["api", f"repos/{owner}/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha"],
        allow_statuses=(404,),
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def create_branch(owner: str, repo: str, branch: str, sha: str) -> bool:
    """Create `branch` pointing at `sha`. Returns False (not an error) if it already exists."""
    result = _rest_call(
        [
            "api", f"repos/{owner}/{repo}/git/refs",
            "-f", f"ref=refs/heads/{branch}",
            "-f", f"sha={sha}",
        ],
        allow_statuses=(422,),
    )
    if result.returncode != 0:
        if "already exists" in (result.stderr or "").lower():
            return False
        raise GhError(
            f"failed to create branch {branch} on {owner}/{repo}: {result.stderr.strip()}",
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        )
    return True


CREATE_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


def create_commit_on_branch(
    owner: str,
    repo: str,
    branch: str,
    message: str,
    additions: dict[str, str],
    base_sha: str,
    deletions: list[str] | None = None,
) -> dict:
    """Commit `additions` (path -> new text content) directly via createCommitOnBranch.

    Handles both new files and full overwrites -- GraphQL doesn't distinguish,
    it just replaces blob contents at `path`.
    """
    file_changes: dict[str, list] = {
        "additions": [
            {"path": path, "contents": base64.b64encode(text.encode("utf-8")).decode("ascii")}
            for path, text in additions.items()
        ],
    }
    if deletions:
        file_changes["deletions"] = [{"path": path} for path in deletions]

    variables = {
        "input": {
            "branch": {
                "repositoryNameWithOwner": f"{owner}/{repo}",
                "branchName": branch,
            },
            "message": {"headline": message},
            "expectedHeadOid": base_sha,
            "fileChanges": file_changes,
        }
    }
    data = gh_graphql(CREATE_COMMIT_MUTATION, variables)
    return data["createCommitOnBranch"]["commit"]


def get_open_pr_for_branch(owner: str, repo: str, branch: str) -> str | None:
    """URL of an existing open PR from `branch`, if any."""
    result = _rest_call(
        [
            "api",
            f"repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open",
            "--jq", ".[0].html_url",
        ],
    )
    url = result.stdout.strip()
    return url or None


def create_pr(owner: str, repo: str, title: str, head: str, base: str, body: str) -> str:
    """Open a PR, or return the URL of an already-open PR from the same branch (idempotent)."""
    existing = get_open_pr_for_branch(owner, repo, head)
    if existing:
        return existing
    result = _rest_call(
        [
            "api", f"repos/{owner}/{repo}/pulls",
            "-f", f"title={title}",
            "-f", f"head={head}",
            "-f", f"base={base}",
            "-f", f"body={body}",
            "--jq", ".html_url",
        ],
        allow_statuses=(422,),
    )
    if result.returncode != 0:
        if "already exists" in (result.stderr or "").lower():
            existing = get_open_pr_for_branch(owner, repo, head)
            if existing:
                return existing
        raise GhError(
            f"failed to create PR for {owner}/{repo}#{head}: {result.stderr.strip()}",
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        )
    return result.stdout.strip()
