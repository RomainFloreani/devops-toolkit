import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import preflight  # noqa: E402
import github_client  # noqa: E402

FIXER_SCRIPT = str(Path(__file__).resolve().parent / "fixtures" / "trim_trailing_whitespace.py")


def _local_config(files_regex: str) -> str:
    return (
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: trim-trailing-whitespace\n"
        "        name: Trim trailing whitespace\n"
        f"        entry: {sys.executable} {FIXER_SCRIPT}\n"
        "        language: system\n"
        f"        files: {files_regex}\n"
    )


def test_no_pre_commit_config_is_a_clean_noop(monkeypatch):
    monkeypatch.setattr(github_client, "fetch_single_file", lambda *a, **k: None)
    result = preflight.run_preflight("o", "r", "main", {"foo.txt": "hello\n"})
    assert result == {"ok": True, "fixed_files": None, "output": ""}


def test_clean_file_passes_with_no_fixes(monkeypatch):
    monkeypatch.setattr(github_client, "fetch_single_file", lambda *a, **k: _local_config(r"\.txt$"))
    result = preflight.run_preflight("o", "r", "main", {"foo.txt": "already clean\n"})
    assert result["ok"] is True
    assert result["fixed_files"] is None


def test_autofix_is_captured_in_fixed_files(monkeypatch):
    monkeypatch.setattr(github_client, "fetch_single_file", lambda *a, **k: _local_config(r"\.txt$"))
    result = preflight.run_preflight("o", "r", "main", {"foo.txt": "trailing spaces   \n"})
    assert result["ok"] is False
    assert result["fixed_files"] == {"foo.txt": "trailing spaces\n"}

    # runner re-runs preflight with the fixed content to confirm a clean pass
    rerun = preflight.run_preflight("o", "r", "main", result["fixed_files"])
    assert rerun["ok"] is True
    assert rerun["fixed_files"] is None
