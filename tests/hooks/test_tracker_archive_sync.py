"""Tests for the tracker-archive-sync PostToolUse hook.

Exercises the hook's pure functions directly (split_closed_rows,
insert_into_archive) rather than shelling out, matching the style of
test_require_worktree_isolation.py.
"""
import importlib.util
from pathlib import Path

_HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "hooks"
    / "tracker-archive-sync.py"
)
_spec = importlib.util.spec_from_file_location("tracker_archive_sync", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _row(tid, status):
    return f"| {tid} | source | title | {status} | branch |"


def test_row_with_fixed_marker_is_moved():
    text = "prose\n" + _row("TRK-001", "fixed@abc123") + "\nmore prose"
    remaining, moved = hook.split_closed_rows(text)
    assert moved == [("TRK-001", _row("TRK-001", "fixed@abc123"))]
    assert "TRK-001" not in remaining


def test_row_with_wontfix_marker_is_moved():
    text = _row("TRK-002", "wontfix: not applicable")
    remaining, moved = hook.split_closed_rows(text)
    assert len(moved) == 1
    assert remaining == ""


def test_row_with_dup_of_marker_is_moved():
    text = _row("TRK-003", "dup-of: TRK-001")
    _remaining, moved = hook.split_closed_rows(text)
    assert moved[0][0] == "TRK-003"


def test_open_row_is_not_moved():
    text = _row("TRK-004", "**OPEN** — needs a follow-up")
    remaining, moved = hook.split_closed_rows(text)
    assert moved == []
    assert "TRK-004" in remaining


def test_narrative_fixed_language_without_the_exact_marker_is_not_moved():
    """The hook must not guess from free-form language -- only the three
    literal markers at the start of the status column count."""
    text = _row("TRK-005", "**FIXED & MERGED** — all gates green")
    remaining, moved = hook.split_closed_rows(text)
    assert moved == []
    assert "TRK-005" in remaining


def test_marker_mentioned_mid_prose_not_at_column_start_is_not_moved():
    """A row whose TITLE column happens to mention 'fixed@' inline (e.g.
    referencing another row's resolution) must not trigger on that mention --
    only a `| fixed@...` at an actual column boundary counts."""
    text = "| TRK-006 | context noting sibling row fixed@deadbeef already | title | **OPEN** | - |"
    remaining, moved = hook.split_closed_rows(text)
    assert moved == []
    assert "TRK-006" in remaining


def test_multiple_rows_mixed_open_and_closed():
    text = "\n".join(
        [
            _row("TRK-010", "**OPEN**"),
            _row("TRK-011", "fixed@111"),
            _row("TRK-012", "**OPEN**"),
            _row("TRK-013", "wontfix: no longer relevant"),
        ]
    )
    remaining, moved = hook.split_closed_rows(text)
    assert [i for i, _ in moved] == ["TRK-011", "TRK-013"]
    assert "TRK-010" in remaining and "TRK-012" in remaining
    assert "TRK-011" not in remaining and "TRK-013" not in remaining


def test_insert_into_archive_places_rows_right_after_table_header():
    archive = (
        "# Archive\n\n"
        "## Resolved / closed findings\n\n"
        "| ID | Origin IDs | Title | Status | Phase |\n"
        "|---|---|---|---|---|\n"
        "| TRK-001 | s | t | fixed@aaa | - |\n"
    )
    moved = [("TRK-002", _row("TRK-002", "fixed@bbb"))]
    result = hook.insert_into_archive(archive, moved)
    lines = result.split("\n")
    header_idx = lines.index("| ID | Origin IDs | Title | Status | Phase |")
    assert lines[header_idx + 2] == _row("TRK-002", "fixed@bbb")
    assert "TRK-001" in result


def test_insert_into_archive_falls_back_to_append_if_marker_missing():
    archive = "# Archive\n\nno matching section header here\n"
    moved = [("TRK-002", _row("TRK-002", "fixed@bbb"))]
    result = hook.insert_into_archive(archive, moved)
    assert result.strip().endswith(_row("TRK-002", "fixed@bbb"))


def test_insert_into_archive_noop_when_nothing_moved():
    archive = "# Archive\n\n## Resolved / closed findings\n\n| ID |\n|---|\n"
    result = hook.insert_into_archive(archive, [])
    assert result == archive
