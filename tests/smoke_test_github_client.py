"""Manual smoke test for github_client.py against one real repo.

Not part of the pytest unit-test suite (it needs live `gh auth login` +
network). Run directly:

    python tests/smoke_test_github_client.py <owner> <repo> <branch> <path>

Example:

    python tests/smoke_test_github_client.py octocat Hello-World master README
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import github_client  # noqa: E402


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    owner, repo, branch, path = sys.argv[1:5]

    print(f"Fetching {owner}/{repo}@{branch}:{path} via batched GraphQL read...")
    result = github_client.fetch_files_batch([(owner, repo, branch, path)])
    text = result[(owner, repo, path)]
    if text is None:
        print("-> file not found (None) -- confirms the 'missing path' path works.")
    else:
        print(f"-> read {len(text)} chars. First 200:\n{text[:200]!r}")

    print(f"\nFetching current tip SHA of {owner}/{repo}@{branch}...")
    sha = github_client.get_branch_tip_sha(owner, repo, branch)
    print(f"-> {sha}")


if __name__ == "__main__":
    main()
