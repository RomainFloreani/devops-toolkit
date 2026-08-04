"""Simulates pre-commit hooks before pushing.

Commits are made via createCommitOnBranch (GraphQL) -- no local `git commit`
ever happens, so a repo's pre-commit hooks never fire naturally. This module
runs them out-of-band, against only the changed file(s), in a throwaway repo.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import github_client

PRE_COMMIT_CONFIG_PATH = ".pre-commit-config.yaml"
PREFLIGHT_TIMEOUT_SECONDS = 180
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
PRE_COMMIT_HOME = TOOLKIT_ROOT / ".cache"


def run_preflight(
    owner: str,
    repo: str,
    branch: str,
    changed_files: dict[str, str],
) -> dict:
    """Run the repo's pre-commit hooks against only `changed_files`.

    Returns {"ok": bool, "fixed_files": dict[path, str] | None, "output": str}.
    `fixed_files` is populated when an autofixing hook (black, prettier,
    end-of-file-fixer, ...) mutated a file in place -- the runner must
    substitute those and re-run preflight once to confirm a clean pass.
    """
    config_text = github_client.fetch_single_file(owner, repo, branch, PRE_COMMIT_CONFIG_PATH)
    if config_text is None:
        return {"ok": True, "fixed_files": None, "output": ""}

    with tempfile.TemporaryDirectory(prefix="preflight-") as tmp:
        tmp_path = Path(tmp)
        _git(tmp_path, ["init", "-q"])
        _git(tmp_path, ["config", "user.email", "devops-toolkit@local"])
        _git(tmp_path, ["config", "user.name", "devops-toolkit"])

        config_path = tmp_path / PRE_COMMIT_CONFIG_PATH
        config_path.write_text(config_text, encoding="utf-8")

        rel_paths = []
        originals: dict[str, str] = {}
        for rel_path, content in changed_files.items():
            file_path = tmp_path / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            originals[rel_path] = content
            rel_paths.append(rel_path)

        _git(tmp_path, ["add", "-A"])

        PRE_COMMIT_HOME.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PRE_COMMIT_HOME": str(PRE_COMMIT_HOME)}
        result = subprocess.run(
            ["pre-commit", "run", "--config", str(config_path), "--files", *rel_paths],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            env=env,
        )
        output = result.stdout + result.stderr

        fixed_files = {}
        for rel_path in rel_paths:
            new_text = (tmp_path / rel_path).read_text(encoding="utf-8")
            if new_text != originals[rel_path]:
                fixed_files[rel_path] = new_text

        ok = result.returncode == 0
        return {"ok": ok, "fixed_files": fixed_files or None, "output": output}


def _git(cwd: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def group_by_config_hash(repos: list[dict]) -> dict[str, list[dict]]:
    """Group repos by the sha256 of their .pre-commit-config.yaml.

    pre-commit's own hook-environment cache (~/.cache/pre-commit, keyed by
    repo+rev) only pays hook-install cost once per distinct config -- this
    lets the runner batch/order work so that cost is paid once per group
    rather than once per repo.
    """
    groups: dict[str, list[dict]] = {}
    for row in repos:
        config_text = github_client.fetch_single_file(
            row["owner"], row["repo"], row["default_branch"], PRE_COMMIT_CONFIG_PATH
        )
        if config_text is None:
            key = "no-config"
        else:
            key = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
        groups.setdefault(key, []).append(row)
    return groups
