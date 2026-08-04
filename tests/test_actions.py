import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import actions  # noqa: E402
from actions import ActionError  # noqa: E402


def test_regex_replace_basic():
    content = "uses: org/central-workflow@v2.1\nother: line\n"
    result = actions.regex_replace(
        content, r"uses:\s*org/central-workflow@v2(\.\d+)?", "uses: org/central-workflow@v3"
    )
    assert result == "uses: org/central-workflow@v3\nother: line\n"


def test_regex_replace_no_match_skips():
    content = "already: v3\n"
    result = actions.regex_replace(content, r"v2", "v3", require_match=False)
    assert result is None


def test_regex_replace_no_match_raises_when_required():
    with pytest.raises(ActionError):
        actions.regex_replace("already: v3\n", r"v2", "v3", require_match=True)


def test_regex_replace_idempotent_when_already_applied():
    content = "uses: org/central-workflow@v3\n"
    result = actions.regex_replace(
        content, r"uses:\s*org/central-workflow@v2(\.\d+)?", "uses: org/central-workflow@v3",
        require_match=False,
    )
    assert result is None


def test_anchor_insert_after():
    content = "steps:\n  - run: setup\n  - run: build\n"
    result = actions.anchor_insert(
        content, anchor_pattern=r"run: setup", lines=["  - run: lint"], position="after"
    )
    assert result == "steps:\n  - run: setup\n  - run: lint\n  - run: build\n"


def test_anchor_insert_before():
    content = "steps:\n  - run: build\n"
    result = actions.anchor_insert(
        content, anchor_pattern=r"run: build", lines=["  - run: lint"], position="before"
    )
    assert result == "steps:\n  - run: lint\n  - run: build\n"


def test_anchor_insert_skip_if_present():
    content = "steps:\n  - run: lint\n  - run: build\n"
    result = actions.anchor_insert(
        content, anchor_pattern=r"run: build", lines=["  - run: lint"],
        position="before", skip_if_present="run: lint",
    )
    assert result is None


def test_anchor_insert_missing_anchor_required_raises():
    with pytest.raises(ActionError):
        actions.anchor_insert("nope\n", anchor_pattern=r"missing", lines=["x"], require_anchor=True)


def test_anchor_insert_missing_anchor_optional_skips():
    result = actions.anchor_insert("nope\n", anchor_pattern=r"missing", lines=["x"], require_anchor=False)
    assert result is None


def test_block_insert_before_end_pattern_after_start():
    content = (
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - run: one\n"
        "  test:\n"
        "    steps:\n"
        "      - run: two\n"
    )
    # insert a new job right before the first job block found after `jobs:`
    result = actions.block_insert(
        content,
        start_pattern=r"^jobs:",
        end_pattern=r"^  build:",
        lines=["  lint:", "    steps:", "      - run: lint"],
        position="before",
    )
    expected = (
        "jobs:\n"
        "  lint:\n"
        "    steps:\n"
        "      - run: lint\n"
        "  build:\n"
        "    steps:\n"
        "      - run: one\n"
        "  test:\n"
        "    steps:\n"
        "      - run: two\n"
    )
    assert result == expected


def test_block_insert_end_pattern_must_be_after_start():
    # end_pattern matches a line that appears *before* start_pattern -- must not match it
    content = "  build:\njobs:\n  test:\n"
    result = actions.block_insert(
        content, start_pattern=r"^jobs:", end_pattern=r"^  build:", lines=["x"], require_match=False,
    )
    assert result is None


def test_block_insert_skip_if_present():
    content = "jobs:\n  lint:\n  build:\n"
    result = actions.block_insert(
        content, start_pattern=r"^jobs:", end_pattern=r"^  build:", lines=["  lint:"],
        skip_if_present="lint:",
    )
    assert result is None


def test_add_file_creates_new():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("template contents\n")
        template_path = f.name
    try:
        result = actions.add_file(None, template_path)
        assert result == "template contents\n"
    finally:
        Path(template_path).unlink()


def test_add_file_raises_if_exists_without_overwrite():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("template contents\n")
        template_path = f.name
    try:
        with pytest.raises(ActionError):
            actions.add_file("existing content", template_path)
    finally:
        Path(template_path).unlink()


def test_add_file_overwrite_allowed():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("template contents\n")
        template_path = f.name
    try:
        result = actions.add_file("existing content", template_path, overwrite=True)
        assert result == "template contents\n"
    finally:
        Path(template_path).unlink()


def test_overwrite_file_idempotent():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("template contents\n")
        template_path = f.name
    try:
        result = actions.overwrite_file("template contents\n", template_path)
        assert result is None
    finally:
        Path(template_path).unlink()


def test_overwrite_file_changes():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("new contents\n")
        template_path = f.name
    try:
        result = actions.overwrite_file("old contents\n", template_path)
        assert result == "new contents\n"
    finally:
        Path(template_path).unlink()
