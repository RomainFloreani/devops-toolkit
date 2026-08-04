# devops-toolkit

Applies standardized changes (add file, edit file, patch specific lines)
across a fleet of GitHub repos, using only the GitHub API via the `gh` CLI --
no `git clone`, no local checkouts. Repos are filtered from a CSV inventory,
changes are committed via GraphQL (`createCommitOnBranch`), and PRs are
opened automatically.

Every write is idempotent (re-running a rule against a repo already in the
target state is a no-op) and every run -- dry or real -- produces an audit
CSV under `state/runs/`.

## Install

From PowerShell, in the toolkit root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Auth rides on your existing `gh` session -- run `gh auth login` once if you
haven't. Nothing in this toolkit reads a PAT or env var token directly.

## Repo layout

```
config/repos.csv       inventory: owner,repo,default_branch,team
rules/schema.yaml       jsonschema for rule files
rules/*.yaml             one file per campaign
templates/pr_body.md.j2  PR body template
src/github_client.py     all gh api / gh graphql calls
src/filters.py           decide which repos a rule applies to
src/actions.py           compute new file content (or None = already applied)
src/preflight.py         simulates pre-commit hooks before pushing
src/runner.py             CLI entrypoint
state/runs/               audit CSVs (gitignored, created at runtime)
.cache/                   pre-commit hook-environment cache (gitignored)
```

## Writing a rule file

A rule is one YAML file under `rules/`. It has five parts:

- `filter`: which repos does this apply to? `path_exists` / `path_absent` /
  `content_match` / `content_absent`, evaluated against one file's content.
- `subset` (optional): narrow further by a `config/repos.csv` column, e.g.
  only `team: platform` repos.
- `source_branch` (optional): overrides `config/repos.csv`'s `default_branch`
  column for every repo in this rule's run -- filter reads, the new branch's
  base tip, and the PR base all use this branch name instead. Omit to use
  each repo's own `default_branch` column (the normal case; different repos
  can each have their own value there). Set this when one campaign needs to
  target e.g. `develop` or `release/x` for every repo regardless of what
  their individual `default_branch` is.
- `action`: how to compute the new file content. See the three examples
  below -- pick the narrowest one that fits, and always set a
  `skip_if_present` / `require_match` guard so a second run is a no-op.
- `branch` / `commit_message`: where the change lands.
- `pr`: title + `body_template` (+ optional free-text `description` passed
  into the template).

Validate against `rules/schema.yaml` implicitly on every run -- `runner.py`
loads and validates the rule before touching GitHub, and fails fast naming
the exact bad field if something's off.

Three worked examples ship in `rules/`:

- **`bump-workflow-v2-to-v3.yaml`** -- `regex_replace`. Swaps a pinned
  version string anywhere it appears on one line.
- **`add-lint-step.yaml`** -- `anchor_insert`. Inserts N lines immediately
  before/after the first line matching `anchor_pattern`, guarded by
  `skip_if_present` so re-runs don't duplicate the step.
- **`add-security-scan-job.yaml`** -- `block_insert`. For the "insert at a
  different line number in every repo" case: anchors on `start_pattern`
  (`^jobs:`), then the first `end_pattern` match *after* it (the shape of a
  job key, not a specific job name), and inserts relative to that -- never a
  raw line number.

## Running

Always dry-run first:

```powershell
python src\runner.py --rule rules\bump-workflow-v2-to-v3.yaml --dry-run
```

This runs the filter stage, computes the diff for every matched repo, prints
it, and writes nothing -- no branch, no commit, no PR. It still writes an
audit CSV (status `dry_run_diff` / `no_change_needed` / `not_matched`) so you
can review the blast radius before committing to it.

Then for real:

```powershell
python src\runner.py --rule rules\bump-workflow-v2-to-v3.yaml --concurrency 8
```

Flags:

- `--rule <path>` (required)
- `--dry-run`
- `--concurrency <n>` (default 8 -- threads, not processes; GraphQL point
  cost is the real limit, not CPU)
- `--repos-csv <path>` (default `config/repos.csv`)

Per matched repo, a real run: computes the new content, runs preflight
(simulated pre-commit against just the changed file(s); if a hook autofixes
in place, the fixed content is substituted and preflight re-run once to
confirm a clean pass -- a genuine validator failure skips that repo and logs
`preflight_failed` instead of opening a PR), creates the branch off the
default branch tip (or reuses it if it already exists, skipping the commit
entirely if the branch already has the target content), commits via
`createCommitOnBranch`, and opens a PR (or reuses the existing open one from
that branch).

## Reading the audit CSV

Every run appends one row per repo to
`state/runs/<rule_name>_<UTC_timestamp>.csv`:

```
owner,repo,matched,preflight_status,branch,pr_url,error
```

- `matched=False, error=not_matched` -- filter didn't select this repo,
  nothing else happened.
- `error=no_change_needed` -- matched, but the action determined the repo is
  already in the target state.
- `error=preflight_failed: ...` -- matched, action computed new content, but
  a pre-commit hook rejected it (e.g. `check-yaml`); no branch/commit/PR was
  created. Fix by hand.
- `error=branch_create_failed / commit_failed / pr_failed: ...` -- a `gh`
  call failed after retries; re-run the rule, it's safe (idempotent).
- `error=""` with a `pr_url` -- success (PR opened or an existing one from a
  prior run was reused).

Anything with a non-empty `error` and `matched=True` is the manual follow-up
list.

## Smoke-testing github_client.py

Before trusting a new rule against 60 repos, sanity-check the low-level
GitHub calls against one real repo:

```powershell
python tests\smoke_test_github_client.py <owner> <repo> <branch> <path>
```

This reads one file via the batched GraphQL path and fetches the branch tip
SHA -- no writes.

## Tests

```powershell
pip install pytest
pytest tests\test_filters.py tests\test_actions.py tests\test_preflight.py
```

`test_filters.py` / `test_actions.py` run entirely offline against fixture
strings. `test_preflight.py` exercises the real `pre-commit` binary against a
local, network-free hook (`tests/fixtures/trim_trailing_whitespace.py`), so
`pre-commit` must be installed (`pip install -r requirements.txt` covers it).
