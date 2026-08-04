"""Checks each fleet PR's CI against its repo's default branch, to catch regressions before approving.

Commits are compared by their GitHub check-runs (Actions, and anything else
reporting through the Checks API). Checks are matched by name after
stripping trigger-context noise -- e.g. a job GitHub shows as
"test (pull_request)" on the PR head and "test (push)" on the default
branch tip is still "test" for comparison purposes, otherwise every PR
would look like it changed tests it never touched.

A PR is "approve": every check that passes on the default branch also
passes on the PR. "regressed": at least one of those checks now fails or
is missing entirely. "pending": nothing has regressed yet, but at least
one of those checks is still running on the PR.

    python src/check_pr_regressions.py --branch chore/bump-workflow-v3
    python src/check_pr_regressions.py --rule rules/bump-workflow-v2-to-v3.yaml
"""
from __future__ import annotations

import argparse
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import github_client
import runner

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent

# push/pull_request are the pair that actually differs between a PR head and
# its default branch tip; the rest are here for the same class of noise.
_TRIGGER_TOKEN = re.compile(
    r"\b(pull_request(_target)?|push|workflow_dispatch|schedule|merge_group)\b", re.IGNORECASE
)
_EMPTY_PARENS = re.compile(r"\(\s*\)")
_SEPARATOR_EDGES = re.compile(r"^[\s/\-_:,]+|[\s/\-_:,]+$")
_WHITESPACE = re.compile(r"\s{2,}")


def normalize_check_name(name: str) -> str:
    """Strip trigger-event noise so the same logical job matches across contexts."""
    stripped = _TRIGGER_TOKEN.sub("", name)
    stripped = _EMPTY_PARENS.sub("", stripped)
    stripped = _SEPARATOR_EDGES.sub("", stripped)
    stripped = _WHITESPACE.sub(" ", stripped).strip()
    return stripped.lower() or name.strip().lower()


def latest_by_name(check_runs: list[dict]) -> dict[str, dict]:
    """Collapse re-run duplicates: keep the most recently started run per normalized name."""
    latest: dict[str, dict] = {}
    for run in check_runs:
        key = normalize_check_name(run["name"])
        current = latest.get(key)
        if current is None or (run.get("started_at") or "") >= (current.get("started_at") or ""):
            latest[key] = run
    return latest


def diff_checks(default_runs: list[dict], pr_runs: list[dict]) -> dict:
    """Compare two check-run sets against each other.

    Only checks that pass on the default branch are considered -- a check
    that's already broken on default isn't something the PR could regress.
    Returns {status, regressed, missing, pending}; status is "approve",
    "regressed", or "pending" (pending only ever wins over "approve").
    """
    default_latest = latest_by_name(default_runs)
    pr_latest = latest_by_name(pr_runs)

    regressed, missing, pending = [], [], []
    for name, run in default_latest.items():
        if run.get("conclusion") != "success":
            continue
        pr_run = pr_latest.get(name)
        if pr_run is None:
            missing.append(name)
        elif pr_run.get("status") != "completed":
            pending.append(name)
        elif pr_run.get("conclusion") != "success":
            regressed.append(name)

    if regressed or missing:
        status = "regressed"
    elif pending:
        status = "pending"
    else:
        status = "approve"
    return {"status": status, "regressed": regressed, "missing": missing, "pending": pending}


def check_repo(owner: str, repo: str, default_branch: str, pr_info: dict) -> dict:
    row = {
        "owner": owner, "repo": repo, "pr_number": pr_info["number"], "pr_url": pr_info["url"],
        "status": "error", "regressed": "", "missing": "", "pending": "", "error": "",
    }

    default_sha = github_client.get_branch_tip_sha(owner, repo, default_branch)
    if default_sha is None:
        row["error"] = f"default branch '{default_branch}' not found"
        return row

    default_runs = github_client.get_check_runs(owner, repo, default_sha)
    pr_runs = github_client.get_check_runs(owner, repo, pr_info["head_sha"])
    diff = diff_checks(default_runs, pr_runs)
    row.update({
        "status": diff["status"],
        "regressed": ", ".join(diff["regressed"]),
        "missing": ", ".join(diff["missing"]),
        "pending": ", ".join(diff["pending"]),
    })
    return row


def write_report_csv(branch: str, rows: list[dict]) -> Path:
    runs_dir = TOOLKIT_ROOT / "state" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "_", branch)
    out_path = runs_dir / f"regressions_{safe_branch}_{timestamp}.csv"
    fields = ["owner", "repo", "pr_number", "pr_url", "status", "regressed", "missing", "pending", "error"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branch", help="PR head branch shared across the fleet, e.g. chore/bump-workflow-v3.")
    parser.add_argument(
        "--rule", help="Read --branch (and the repo subset) from this rule file instead of passing --branch."
    )
    parser.add_argument("--repos-csv", default="config/repos.csv", help="Path to the repo inventory CSV.")
    parser.add_argument("--concurrency", type=int, default=8, help="Repos checked in parallel.")
    args = parser.parse_args()

    if not args.branch and not args.rule:
        parser.error("pass --branch or --rule")

    repos = runner.load_repos(Path(args.repos_csv))
    branch = args.branch
    if args.rule:
        rule = runner.load_rule(Path(args.rule))
        repos = runner.apply_subset(repos, rule.get("subset"))
        branch = branch or rule["branch"]

    print(f"looking for open PRs on '{branch}' across {len(repos)} repo(s)...")
    pr_map = github_client.fetch_open_prs_by_branch([(r["owner"], r["repo"]) for r in repos], branch)
    targets = [
        (r["owner"], r["repo"], r["default_branch"], pr_map[(r["owner"], r["repo"])])
        for r in repos if pr_map.get((r["owner"], r["repo"]))
    ]
    print(f"{len(targets)}/{len(repos)} repo(s) have an open PR on '{branch}'")
    if not targets:
        return

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(check_repo, owner, repo, default_branch, pr_info): (owner, repo)
            for owner, repo, default_branch, pr_info in targets
        }
        for future in as_completed(futures):
            owner, repo = futures[future]
            try:
                row = future.result()
            except Exception as e:  # noqa: BLE001 - surface unexpected errors per-repo, don't kill the run
                row = {"owner": owner, "repo": repo, "pr_number": "", "pr_url": "", "status": "error",
                       "regressed": "", "missing": "", "pending": "", "error": f"unexpected_error: {e}"}
            rows.append(row)

    approve = [r for r in rows if r["status"] == "approve"]
    regressed = [r for r in rows if r["status"] == "regressed"]
    pending = [r for r in rows if r["status"] == "pending"]
    errored = [r for r in rows if r["status"] == "error"]

    print(f"\ngood to approve ({len(approve)}):")
    for r in approve:
        print(f"  [{r['owner']}/{r['repo']}] {r['pr_url']}")

    print(f"\nregressed ({len(regressed)}):")
    for r in regressed:
        detail = "; ".join(p for p in (
            f"failed: {r['regressed']}" if r["regressed"] else "",
            f"missing: {r['missing']}" if r["missing"] else "",
        ) if p)
        print(f"  [{r['owner']}/{r['repo']}] {r['pr_url']} -- {detail}")

    if pending:
        print(f"\nstill running, not yet approvable ({len(pending)}):")
        for r in pending:
            print(f"  [{r['owner']}/{r['repo']}] {r['pr_url']} -- pending: {r['pending']}")

    if errored:
        print(f"\ncouldn't check ({len(errored)}):")
        for r in errored:
            print(f"  [{r['owner']}/{r['repo']}] {r['error']}")

    out_path = write_report_csv(branch, rows)
    print(f"\nreport written to {out_path}")


if __name__ == "__main__":
    main()
