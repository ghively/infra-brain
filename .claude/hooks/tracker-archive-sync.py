#!/usr/bin/env python3
"""PostToolUse hook: after any edit to docs/TRACKER.md, auto-move rows that
were just closed into docs/TRACKER-ARCHIVE.md so the active tracker only
ever holds current/future work (the maintainer's request, 2026-07-30 tracker split).

Contract (also stated in docs/TRACKER.md's own rules): a row is "closed" only
when its Status column (4th `|`-delimited field) begins with exactly one of:
  fixed@<commit>   wontfix: <reason>   dup-of: <ID>
This mirrors the pre-existing closure convention -- the hook doesn't guess
from free-form language (that requires human/LLM judgment, see the 4-agent
review pass that did the initial 2026-07-30 split), it only recognizes the
already-documented explicit markers. A row written with narrative status text
("FIXED & MERGED", "PASS", ...) stays active until edited to use one of the
three markers, or archived by hand / a future bulk pass.
"""
import json
import re
import sys
from pathlib import Path

CLOSED_RE = re.compile(r"\| (?:fixed@|wontfix:|dup-of:)")
ROW_RE = re.compile(r"^\| (TRK-\d+) \|")
RESOLVED_SECTION_MARKER = "## Resolved / closed findings\n\n"


def split_closed_rows(tracker_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (remaining_tracker_text, [(id, row_line), ...]) for closed rows."""
    lines = tracker_text.split("\n")
    kept_lines = []
    moved_rows = []
    for line in lines:
        m = ROW_RE.match(line)
        if m and CLOSED_RE.search(line):
            moved_rows.append((m.group(1), line))
        else:
            kept_lines.append(line)
    return "\n".join(kept_lines), moved_rows


def insert_into_archive(archive_text: str, moved_rows: list[tuple[str, str]]) -> str:
    """Insert moved rows right after the resolved-table header in archive_text."""
    if not moved_rows:
        return archive_text
    insertion = "\n".join(line for _id, line in moved_rows) + "\n"
    idx = archive_text.find(RESOLVED_SECTION_MARKER)
    if idx == -1:
        # Fallback: append at end of file if the expected section header moved/renamed.
        return archive_text.rstrip("\n") + "\n\n" + insertion
    after_marker = idx + len(RESOLVED_SECTION_MARKER)
    header_line_end = archive_text.find("\n", after_marker) + 1
    separator_line_end = archive_text.find("\n", header_line_end) + 1
    return archive_text[:separator_line_end] + insertion + archive_text[separator_line_end:]


def main() -> int:
    data = json.load(sys.stdin)
    fp = data.get("tool_input", {}).get("file_path", "")

    if not fp.replace("\\", "/").endswith("docs/TRACKER.md"):
        return 0

    tracker_path = Path(fp)
    archive_path = tracker_path.parent / "TRACKER-ARCHIVE.md"
    if not archive_path.exists():
        return 0

    tracker_text = tracker_path.read_text()
    new_tracker_text, moved_rows = split_closed_rows(tracker_text)
    if not moved_rows:
        return 0

    tracker_path.write_text(new_tracker_text)
    archive_path.write_text(insert_into_archive(archive_path.read_text(), moved_rows))

    ids = ", ".join(i for i, _ in moved_rows)
    print(f"[tracker-archive-sync] moved {len(moved_rows)} closed row(s) to TRACKER-ARCHIVE.md: {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
