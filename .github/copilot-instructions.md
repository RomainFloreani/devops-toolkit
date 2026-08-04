# devops-toolkit -- Copilot instructions

Applies standardized changes across a fleet of GitHub repos listed in
`config/repos.csv`, using only the `gh` CLI (GraphQL `createCommitOnBranch`
for writes) -- there is no `git clone` and no local checkout of any target
repo anywhere in this codebase. Keep it that way: any change that reads or
writes a fleet repo's content must go through `src/github_client.py`.

## Repo layout

```
config/repos.csv        inventory: owner,repo,default_branch,team
rules/schema.yaml        jsonschema for rule files -- the source of truth for valid rule shape
rules/*.yaml              one file per campaign, validated against schema.yaml on every run
templates/pr_body.md.j2   PR body template (Jinja)
templates/*.default       static file bodies for add_file / overwrite_file actions (NOT Jinja-rendered)
src/github_client.py      all gh api / gh graphql calls -- the only place network I/O happens
src/filters.py            pure functions: decide which repos a rule applies to
src/actions.py            pure functions: compute new file content (or None = already applied)
src/preflight.py          simulates the target repo's own pre-commit hooks before pushing
src/runner.py             CLI entrypoint, wires the above together
state/runs/                audit CSVs (gitignored, created at runtime)
```

`src/filters.py` and `src/actions.py` are intentionally pure (no GitHub
reads/writes) -- `src/runner.py` and `src/github_client.py` do all the I/O
and pass plain strings in. Keep new filter/action logic pure too; it's what
makes `tests/test_filters.py` / `tests/test_actions.py` able to run fully
offline against fixture strings.

## The five-part rule file

A rule is one YAML file under `rules/`, validated against `rules/schema.yaml`
(`runner.py` fails fast, naming the exact bad field, before touching
GitHub):

- `filter` -- which repos does this apply to. Types: `path_exists`,
  `path_absent`, `content_match`, `content_absent` (all evaluated against one
  file's content), `pr_open_on_branch` (matches an already-open PR head, not
  file content), `all_of` (narrows repo-by-repo through an ordered list of
  the above -- put the cheapest filter first).
- `subset` (optional) -- narrow further by a `config/repos.csv` column, e.g.
  `{column: team, equals: platform}`.
- `source_branch` (optional) -- overrides `config/repos.csv`'s
  `default_branch` for every repo in this rule's run (filter reads, new
  branch's base tip, PR base). Omit unless the campaign must target one
  branch name (e.g. `develop`) across repos whose own `default_branch`
  column differs.
- `action` -- how to compute the new content. Types: `add_file` /
  `overwrite_file` (whole-file, from `source_template_path`, no
  templating), `regex_replace`, `anchor_insert` (N lines before/after the
  first `anchor_pattern` match), `block_insert` (N lines before/after the
  first `end_pattern` match found *after* `start_pattern` -- for "insert at
  a line number that differs per repo"). Pick the narrowest action that
  fits the change.
- `branch` / `commit_message` -- where the change lands. Reusing an
  existing open branch/PR (e.g. one left open by an earlier related rule)
  is a supported, intentional pattern -- see `note-open-devops-branch-in-
  changelog.yaml`.
- `pr` -- `title` + `body_template` (+ optional free-text `description`
  interpolated into the template).

**Every action must be idempotent**: a second run against a repo already in
the target state must return `None` (`no_change_needed`), never re-apply or
error. Always set a `skip_if_present` / `require_match` guard (or rely on
`regex_replace`'s built-in old==new check). For `regex_replace` /
`anchor_insert` / `block_insert`, only set `require_match: true` /
`require_anchor: true` when the *filter* already guarantees the pattern is
present -- otherwise a mismatch is a legitimate "this repo doesn't need the
change" case, not a bug, and should degrade to a clean skip instead of an
`action_error`. See `bump-node-engines-lts.yaml` for a broad-filter case
that relies on `require_match: false` for exactly this reason.

`regex_replace`'s `replacement` and `anchor_insert`/`block_insert`'s `lines`
support per-repo Jinja placeholders (`{{ owner }}`, `{{ repo }}`,
`{{ default_branch }}`, `{{ branch }}`), rendered once per repo before the
action runs. `add_file`/`overwrite_file`'s `source_template_path` content is
read verbatim -- no Jinja there.

Eleven worked examples cover every filter type, every action type, both
insert positions, and every rule-level option at least once -- copy the
nearest match in `rules/` rather than writing a rule from scratch.

## Workflow conventions

- **Always dry-run first**: `python src/runner.py --rule rules/x.yaml
  --dry-run`. This runs filter + computes every matched repo's diff and
  writes nothing to GitHub (still writes an audit CSV under `state/runs/`).
  Note `--dry-run` does **not** exercise `src/preflight.py` -- a rule that
  passes dry-run can still hit `preflight_failed` on the real run if a
  target repo's own pre-commit hooks reject the change.
- The shell examples throughout this repo (README, comments) are
  PowerShell (`.\.venv\Scripts\Activate.ps1`, backslash paths) -- match
  that convention in any new docs or scripts, even though the toolkit
  itself is plain cross-platform Python.
- Auth rides on the caller's own `gh auth login` session. Never add a PAT,
  token env var, or credential file anywhere in this codebase.
- Run tests with `pytest tests/test_filters.py tests/test_actions.py
  tests/test_preflight.py`. The first two are fully offline; the third
  shells out to a real `pre-commit` binary against a local, network-free
  hook fixture (`tests/fixtures/trim_trailing_whitespace.py`).
