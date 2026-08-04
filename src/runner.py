"""CLI entrypoint. Run from the toolkit root:

    python src/runner.py --rule rules/bump-workflow-v2-to-v3.yaml --dry-run
    python src/runner.py --rule rules/bump-workflow-v2-to-v3.yaml
"""
from __future__ import annotations

import argparse
import csv
import difflib
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import yaml
from jsonschema import Draft7Validator
from jsonschema.exceptions import best_match

import actions
import filters
import github_client
import preflight

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = TOOLKIT_ROOT / "rules" / "schema.yaml"
CSV_FIELDS = ["owner", "repo", "matched", "preflight_status", "branch", "pr_url", "error"]

# Action content fields that may contain per-repo Jinja placeholders
# ({{ owner }}, {{ repo }}, {{ default_branch }}, {{ branch }}), rendered
# once per repo before the action runs -- e.g. replacement: "autoupdate_branch:
# {{ default_branch }}" so every repo gets its own branch name instead of one
# fixed string shared by the whole rule.
TEMPLATABLE_ACTION_FIELDS = {
    "regex_replace": ["replacement"],
    "anchor_insert": ["lines"],
    "block_insert": ["lines"],
}


def render_action_context(action_cfg: dict, owner: str, repo: str, default_branch: str, branch: str) -> dict:
    fields = TEMPLATABLE_ACTION_FIELDS.get(action_cfg["type"], [])
    if not fields:
        return action_cfg
    context = {"owner": owner, "repo": repo, "default_branch": default_branch, "branch": branch}
    rendered = dict(action_cfg)
    for field in fields:
        value = rendered[field]
        if isinstance(value, list):
            rendered[field] = [jinja2.Template(item).render(**context) for item in value]
        else:
            rendered[field] = jinja2.Template(value).render(**context)
    return rendered


def resolve_filter_matches(filter_cfg: dict, repos: list[dict]) -> list[tuple[str, str, str]]:
    """Evaluate `filter_cfg` against `repos`, doing whichever GitHub fetch it needs.

    `all_of` filters are evaluated in order, each stage only fetching for
    repos that survived the previous one -- narrowing, not an independent
    fetch-then-intersect, so an early cheap stage (e.g. pr_open_on_branch)
    keeps a later expensive one (a big content_match batch) small.
    """
    filter_type = filter_cfg["type"]

    if filter_type == "all_of":
        candidates = repos
        for sub_filter in filter_cfg["filters"]:
            matched = resolve_filter_matches(sub_filter, candidates)
            matched_keys = {(o, r) for o, r, _b in matched}
            candidates = [row for row in candidates if (row["owner"], row["repo"]) in matched_keys]
            if not candidates:
                break
        return [(row["owner"], row["repo"], row["default_branch"]) for row in candidates]

    if filter_type == "pr_open_on_branch":
        pr_state = github_client.fetch_open_pr_branches(
            [(r["owner"], r["repo"]) for r in repos], filter_cfg["branch"],
        )
        return filters.apply_filter(filter_cfg, repos, pr_state)

    path = filter_cfg["path"]
    items = [(r["owner"], r["repo"], r["default_branch"], path) for r in repos]
    fetched_raw = github_client.fetch_files_batch(items)
    fetched = {(o, r): text for (o, r, _p), text in fetched_raw.items()}
    return filters.apply_filter(filter_cfg, repos, fetched)


def describe_filter(filter_cfg: dict) -> str:
    filter_type = filter_cfg["type"]
    if filter_type == "all_of":
        return " AND ".join(describe_filter(f) for f in filter_cfg["filters"])
    if filter_type == "pr_open_on_branch":
        return f"pr_open_on_branch({filter_cfg['branch']})"
    return f"{filter_type}({filter_cfg['path']})"


def load_rule(rule_path: Path) -> dict:
    with open(rule_path, "r", encoding="utf-8") as f:
        rule = yaml.safe_load(f)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = yaml.safe_load(f)

    validator = Draft7Validator(schema)
    errors = list(validator.iter_errors(rule))
    if errors:
        error = best_match(errors)
        field = "/".join(str(p) for p in error.absolute_path) or "<root>"
        print(f"error: rule file {rule_path} is invalid at '{field}': {error.message}", file=sys.stderr)
        sys.exit(1)
    return rule


def load_repos(csv_path: Path) -> list[dict]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def apply_subset(repos: list[dict], subset_cfg: dict | None) -> list[dict]:
    if not subset_cfg:
        return repos
    column, equals = subset_cfg["column"], subset_cfg["equals"]
    return [r for r in repos if r.get(column) == equals]


def render_pr_body(rule: dict, owner: str, repo: str) -> str:
    template_path = Path(rule["pr"]["body_template"])
    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read()
    return jinja2.Template(template_text).render(
        rule_name=rule["name"],
        repo=f"{owner}/{repo}",
        description=rule["pr"].get("description", ""),
    )


def make_row(owner: str, repo: str, matched: bool, preflight_status: str,
             branch: str, pr_url: str, error: str) -> dict:
    return {
        "owner": owner, "repo": repo, "matched": matched,
        "preflight_status": preflight_status, "branch": branch,
        "pr_url": pr_url, "error": error,
    }


def process_repo(rule: dict, owner: str, repo: str, source_branch: str, before: str | None) -> dict:
    action_path = rule["action"]["path"]
    target_branch = rule["branch"]
    action_cfg = render_action_context(rule["action"], owner, repo, source_branch, target_branch)

    try:
        after = actions.run_action(action_cfg, before)
    except actions.ActionError as e:
        return make_row(owner, repo, True, "not_run", target_branch, "", f"action_error: {e}")

    if after is None:
        return make_row(owner, repo, True, "not_run", target_branch, "", "no_change_needed")

    changed_files = {action_path: after}
    pf = preflight.run_preflight(owner, repo, source_branch, changed_files)

    if not pf["ok"] and not pf["fixed_files"]:
        return make_row(owner, repo, True, "failed", target_branch, "",
                         f"preflight_failed: {pf['output'][:500]}")

    preflight_status = "ok"
    if pf["fixed_files"]:
        changed_files.update(pf["fixed_files"])
        pf2 = preflight.run_preflight(owner, repo, source_branch, changed_files)
        if not pf2["ok"]:
            return make_row(owner, repo, True, "failed", target_branch, "",
                             f"preflight_failed_after_fix: {pf2['output'][:500]}")
        preflight_status = "fixed"
        after = changed_files[action_path]

    tip_sha = github_client.get_branch_tip_sha(owner, repo, source_branch)
    if tip_sha is None:
        return make_row(owner, repo, True, preflight_status, target_branch, "",
                         f"default branch '{source_branch}' not found")

    try:
        created = github_client.create_branch(owner, repo, target_branch, tip_sha)
    except github_client.GhError as e:
        return make_row(owner, repo, True, preflight_status, target_branch, "", f"branch_create_failed: {e}")

    if created:
        base_sha = tip_sha
    else:
        # Branch already exists (earlier run). If its tip already has our
        # target content, skip the commit -- idempotent no-op -- and just
        # make sure a PR exists.
        existing = github_client.fetch_single_file(owner, repo, target_branch, action_path)
        if existing == after:
            try:
                body = render_pr_body(rule, owner, repo)
                pr_url = github_client.create_pr(
                    owner, repo, rule["pr"]["title"], target_branch, source_branch, body,
                )
            except github_client.GhError as e:
                return make_row(owner, repo, True, preflight_status, target_branch, "", f"pr_failed: {e}")
            return make_row(owner, repo, True, preflight_status, target_branch, pr_url, "")
        base_sha = github_client.get_branch_tip_sha(owner, repo, target_branch)

    try:
        github_client.create_commit_on_branch(
            owner, repo, target_branch, rule["commit_message"], {action_path: after}, base_sha,
        )
    except github_client.GhError as e:
        return make_row(owner, repo, True, preflight_status, target_branch, "", f"commit_failed: {e}")

    try:
        body = render_pr_body(rule, owner, repo)
        pr_url = github_client.create_pr(
            owner, repo, rule["pr"]["title"], target_branch, source_branch, body,
        )
    except github_client.GhError as e:
        return make_row(owner, repo, True, preflight_status, target_branch, "", f"pr_failed: {e}")

    return make_row(owner, repo, True, preflight_status, target_branch, pr_url, "")


def write_audit_csv(rule_name: str, rows: list[dict]) -> Path:
    runs_dir = TOOLKIT_ROOT / "state" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = runs_dir / f"{rule_name}_{timestamp}.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-repo GitHub automation via gh API only.")
    parser.add_argument("--rule", required=True, help="Path to a rule YAML file.")
    parser.add_argument("--dry-run", action="store_true", help="Filter + diff only, write nothing.")
    parser.add_argument("--concurrency", type=int, default=8, help="Thread pool size for the write stage.")
    parser.add_argument("--repos-csv", default="config/repos.csv", help="Path to the repo inventory CSV.")
    args = parser.parse_args()

    rule = load_rule(Path(args.rule))
    repos = load_repos(Path(args.repos_csv))
    repos = apply_subset(repos, rule.get("subset"))

    source_branch_override = rule.get("source_branch")
    if source_branch_override:
        for r in repos:
            r["default_branch"] = source_branch_override

    action_path = rule["action"]["path"]
    filter_cfg = rule["filter"]

    matched = resolve_filter_matches(filter_cfg, repos)
    matched_keys = {(o, r) for o, r, _b in matched}

    # Fetched fresh against just the (already-narrowed) matched set, rather
    # than reused from the filter stage -- with all_of / pr_open_on_branch
    # filters there isn't always a single well-defined "filter_path" to reuse
    # from, and refetching only for matched repos is no more expensive than
    # the old reuse path was for the common single-content-filter case.
    action_items = [(o, r, b, action_path) for o, r, b in matched]
    action_fetched_raw = github_client.fetch_files_batch(action_items)
    action_fetched = {(o, r): text for (o, r, _p), text in action_fetched_raw.items()}

    rows: list[dict] = []
    for row in repos:
        key = (row["owner"], row["repo"])
        if key not in matched_keys:
            rows.append(make_row(row["owner"], row["repo"], False, "not_run", "", "", "not_matched"))

    print(f"{len(matched)}/{len(repos)} repos matched filter: {describe_filter(filter_cfg)}")

    if args.dry_run:
        for owner, repo, branch in matched:
            before = action_fetched.get((owner, repo))
            action_cfg = render_action_context(rule["action"], owner, repo, branch, rule["branch"])
            try:
                after = actions.run_action(action_cfg, before)
            except actions.ActionError as e:
                rows.append(make_row(owner, repo, True, "not_run", rule["branch"], "", f"action_error: {e}"))
                print(f"[{owner}/{repo}] action_error: {e}")
                continue
            if after is None:
                rows.append(make_row(owner, repo, True, "not_run", rule["branch"], "", "no_change_needed"))
                print(f"[{owner}/{repo}] no change needed")
                continue
            diff = difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"{owner}/{repo}:{action_path} (before)",
                tofile=f"{owner}/{repo}:{action_path} (after)",
            )
            print("".join(diff))
            rows.append(make_row(owner, repo, True, "not_run", rule["branch"], "", "dry_run_diff"))
        out_path = write_audit_csv(rule["name"], rows)
        print(f"dry-run complete, wrote nothing to GitHub. audit: {out_path}")
        return

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(process_repo, rule, owner, repo, branch, action_fetched.get((owner, repo))): (owner, repo)
            for owner, repo, branch in matched
        }
        for future in as_completed(futures):
            owner, repo = futures[future]
            try:
                row = future.result()
            except Exception as e:  # noqa: BLE001 - surface unexpected errors per-repo, don't kill the run
                row = make_row(owner, repo, True, "failed", rule["branch"], "", f"unexpected_error: {e}")
            rows.append(row)
            status = row["error"] or "ok"
            print(f"[{owner}/{repo}] {status}" + (f" -> {row['pr_url']}" if row["pr_url"] else ""))

    out_path = write_audit_csv(rule["name"], rows)
    ok_count = sum(1 for r in rows if r["matched"] and not r["error"])
    print(f"done: {ok_count} PRs opened/verified, {len(rows) - ok_count} skipped/failed. audit: {out_path}")


if __name__ == "__main__":
    main()
