"""Repo filters. Pure functions over already-fetched file text -- no GitHub
reads happen here, that's github_client.py's job.
"""
from __future__ import annotations

import re


def path_exists(text: str | None) -> bool:
    return text is not None


def path_absent(text: str | None) -> bool:
    return text is None


def content_match(text: str | None, pattern: str) -> bool:
    return text is not None and re.search(pattern, text) is not None


def content_absent(text: str | None, pattern: str) -> bool:
    return text is None or re.search(pattern, text) is None


FILTERS = {
    "path_exists": path_exists,
    "path_absent": path_absent,
    "content_match": content_match,
    "content_absent": content_absent,
}


def apply_filter(
    filter_cfg: dict,
    repos: list[dict],
    fetched: dict[tuple[str, str], str | None],
) -> list[tuple[str, str, str]]:
    """Evaluate `filter_cfg` against every repo in `repos`.

    `fetched` is keyed by (owner, repo) -> text_or_None for filter_cfg["path"]
    (already batch-read by github_client.fetch_files_batch).
    Returns matched (owner, repo, default_branch) tuples.
    """
    filter_type = filter_cfg["type"]
    fn = FILTERS[filter_type]
    needs_pattern = filter_type in ("content_match", "content_absent")

    matched = []
    for row in repos:
        key = (row["owner"], row["repo"])
        text = fetched.get(key)
        ok = fn(text, filter_cfg["pattern"]) if needs_pattern else fn(text)
        if ok:
            matched.append((row["owner"], row["repo"], row["default_branch"]))
    return matched
