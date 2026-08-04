"""Content-transform actions. Pure functions: current content in, new content
(or None for "already applied, skip") out. No GitHub I/O happens here.
"""
from __future__ import annotations

import re


class ActionError(Exception):
    """A genuine action failure (bad rule/repo pairing), as opposed to the
    "no change needed" None-return idempotency signal."""


def add_file(content: str | None, source_template_path: str, overwrite: bool = False) -> str | None:
    if content is not None and not overwrite:
        raise ActionError(
            f"add_file: target already exists and overwrite is not set "
            f"(source_template_path={source_template_path!r})"
        )
    with open(source_template_path, "r", encoding="utf-8") as f:
        new_content = f.read()
    if content == new_content:
        return None
    return new_content


def overwrite_file(content: str | None, source_template_path: str) -> str | None:
    with open(source_template_path, "r", encoding="utf-8") as f:
        new_content = f.read()
    if content == new_content:
        return None
    return new_content


def regex_replace(content: str | None, pattern: str, replacement: str, require_match: bool = True) -> str | None:
    if content is None:
        if require_match:
            raise ActionError(f"regex_replace: target file does not exist (pattern={pattern!r})")
        return None
    new_content, count = re.subn(pattern, replacement, content)
    if count == 0:
        if require_match:
            raise ActionError(f"regex_replace: pattern not found: {pattern!r}")
        return None
    if new_content == content:
        return None
    return new_content


def fix_line_endings(content: str | None) -> str | None:
    """Forces LF and a single trailing newline, unconditionally.

    Equivalent to mixed-line-ending --fix=lf + end-of-file-fixer combined,
    not their default --fix=auto behavior -- a file that's already 100% CRLF
    passes default mixed-line-ending untouched (CRLF is "the dominant"
    ending in it), which is exactly the gap this closes.
    """
    if content is None:
        return None
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized:
        normalized = normalized.rstrip("\n") + "\n"
    if normalized == content:
        return None
    return normalized


def _find_line(lines: list[str], pattern: str, start: int = 0) -> int | None:
    regex = re.compile(pattern)
    for i in range(start, len(lines)):
        if regex.search(lines[i]):
            return i
    return None


def _rejoin(original: str, lines: list[str]) -> str:
    new_content = "\n".join(lines)
    if original.endswith("\n"):
        new_content += "\n"
    return new_content


def anchor_insert(
    content: str | None,
    anchor_pattern: str,
    lines: list[str],
    position: str = "after",
    skip_if_present: str | None = None,
    require_anchor: bool = True,
) -> str | None:
    if content is None:
        if require_anchor:
            raise ActionError(f"anchor_insert: target file does not exist (anchor_pattern={anchor_pattern!r})")
        return None
    if skip_if_present and skip_if_present in content:
        return None

    original_lines = content.splitlines()
    idx = _find_line(original_lines, anchor_pattern)
    if idx is None:
        if require_anchor:
            raise ActionError(f"anchor_insert: anchor pattern not found: {anchor_pattern!r}")
        return None

    insert_at = idx + 1 if position == "after" else idx
    new_lines = original_lines[:insert_at] + list(lines) + original_lines[insert_at:]
    return _rejoin(content, new_lines)


def block_insert(
    content: str | None,
    start_pattern: str,
    end_pattern: str,
    lines: list[str],
    position: str = "before",
    require_match: bool = True,
    skip_if_present: str | None = None,
) -> str | None:
    if content is None:
        if require_match:
            raise ActionError(f"block_insert: target file does not exist (start_pattern={start_pattern!r})")
        return None
    if skip_if_present and skip_if_present in content:
        return None

    original_lines = content.splitlines()
    start_idx = _find_line(original_lines, start_pattern)
    if start_idx is None:
        if require_match:
            raise ActionError(f"block_insert: start pattern not found: {start_pattern!r}")
        return None

    end_idx = _find_line(original_lines, end_pattern, start=start_idx + 1)
    if end_idx is None:
        if require_match:
            raise ActionError(f"block_insert: end pattern not found after start: {end_pattern!r}")
        return None

    insert_at = end_idx if position == "before" else end_idx + 1
    new_lines = original_lines[:insert_at] + list(lines) + original_lines[insert_at:]
    return _rejoin(content, new_lines)


ACTIONS = {
    "add_file": add_file,
    "overwrite_file": overwrite_file,
    "regex_replace": regex_replace,
    "anchor_insert": anchor_insert,
    "block_insert": block_insert,
    "fix_line_endings": fix_line_endings,
}


def run_action(action_cfg: dict, content: str | None) -> str | None:
    fn = ACTIONS[action_cfg["type"]]
    params = {k: v for k, v in action_cfg.items() if k not in ("type", "path")}
    return fn(content, **params)
