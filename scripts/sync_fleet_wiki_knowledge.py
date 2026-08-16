"""One-off, manually-run sync: fleet-wiki curated markdown -> infra-brain repo tree.

Copies curated knowledge-base markdown from the sibling local repo
``~/git/fleet-wiki`` into infra-brain's own repo tree under
``docs/knowledge-sync/``, in a shape ``LocalDocsAgent``
(``src/infra_brain/agents/local_docs.py``) already recognizes via its
``_ALLOW_GLOBS`` entry for ``docs/knowledge-sync/**/*.md`` — so the next
scheduled LocalDocsAgent run ingests this content into the RAG store
(``Document``/``DocumentChunk``) with no further wiring.

This is NOT a new scheduled agent. It is a standalone, dependency-free script
you run by hand whenever you want to refresh the synced content. Sources:

  wiki/runbooks/**/*.md    -> docs/knowledge-sync/wiki-runbooks/
  wiki/incidents/**/*.md   -> docs/knowledge-sync/wiki-incidents/
  wiki/compliance/**/*.md  -> docs/knowledge-sync/wiki-compliance/
  wiki/concepts/**/*.md    -> docs/knowledge-sync/wiki-concepts/
  raw/confluence/**/*.md   -> docs/knowledge-sync/confluence/ (space-code subdirs preserved)

``wiki/*`` sources use YAML frontmatter; any file whose frontmatter ``tags:``
list contains ``auto-generated``, or whose body contains the literal string
``Stub page — auto-created`` or ``Auto-stubbed by``, is a placeholder stub and
is skipped. ``raw/confluence/**/*.md`` files are raw exports with no such
frontmatter shape — copied unconditionally.

Idempotent: a destination file that already has identical content (sha256
match) is left untouched — re-running after fleet-wiki changes is cheap.

Per this project's F-007 convention ("never silently drop" — see
``local_docs.py``'s own ``collect()`` error handling), a per-file read
failure is logged and counted as an error; it never aborts the whole run.

READ FIRST: run with --dry-run (default, the default action) to see counts;
pass --apply to actually copy.
Usage:  python scripts/sync_fleet_wiki_knowledge.py [--dry-run | --apply]
                                                     [--source-root PATH] [--dest-root PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# (source-relative glob under --source-root, dest subdir name under --dest-root,
#  whether stub-filtering applies).
_SOURCES: tuple[tuple[str, str, bool], ...] = (
    ("wiki/runbooks", "wiki-runbooks", True),
    ("wiki/incidents", "wiki-incidents", True),
    ("wiki/compliance", "wiki-compliance", True),
    ("wiki/concepts", "wiki-concepts", True),
    ("raw/confluence", "confluence", False),
)

_STUB_BODY_MARKERS: tuple[str, ...] = ("Stub page — auto-created", "Auto-stubbed by")


@dataclass
class SourceStats:
    label: str
    copied: int = 0
    would_copy: int = 0
    unchanged: int = 0
    stub_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _is_stub(text: str) -> bool:
    """True if this wiki/* file is an auto-generated placeholder, per the
    frontmatter ``tags: [..., auto-generated, ...]`` or body-marker rule."""
    if any(marker in text for marker in _STUB_BODY_MARKERS):
        return True
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    frontmatter = text[3:end]
    # Manual delimiter split + substring check (no new YAML dependency for a
    # one-off script) — look for `auto-generated` inside the `tags:` block.
    tags_idx = frontmatter.find("tags:")
    if tags_idx == -1:
        return False
    # tags: block runs until the next top-level (non-indented, non-"- ") key.
    rest = frontmatter[tags_idx:]
    lines = rest.splitlines()
    block_lines = [lines[0]]
    for line in lines[1:]:
        if line.startswith(("-", " ", "\t")) or not line.strip():
            block_lines.append(line)
        else:
            break
    return "auto-generated" in "\n".join(block_lines)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_one_source(
    source_dir: Path, dest_dir: Path, label: str, stub_filter: bool, apply: bool
) -> SourceStats:
    stats = SourceStats(label=label)
    if not source_dir.is_dir():
        stats.errors.append(f"source dir does not exist: {source_dir}")
        return stats

    for path in sorted(source_dir.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            stats.errors.append(f"read failed for {path}: {exc}")
            continue

        if not raw:
            stats.errors.append(f"empty file skipped: {path}")
            continue

        if stub_filter:
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:  # pragma: no cover - decode with errors="replace" won't raise
                stats.errors.append(f"decode failed for {path}: {exc}")
                continue
            if _is_stub(text):
                stats.stub_skipped += 1
                continue

        dest_path = dest_dir / rel
        if dest_path.is_file():
            try:
                existing = dest_path.read_bytes()
            except OSError as exc:
                stats.errors.append(f"read failed for existing dest {dest_path}: {exc}")
                continue
            if _sha256(existing) == _sha256(raw):
                stats.unchanged += 1
                continue

        if apply:
            try:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_path)
            except OSError as exc:
                stats.errors.append(f"write failed for {dest_path}: {exc}")
                continue
            stats.copied += 1
        else:
            stats.would_copy += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print counts only (default).")
    mode.add_argument("--apply", action="store_true", help="Actually copy files.")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/home/youruser/git/fleet-wiki"),
        help="Root of the fleet-wiki repo (default: /home/youruser/git/fleet-wiki).",
    )
    parser.add_argument(
        "--dest-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "knowledge-sync",
        help="Destination root inside infra-brain (default: <repo>/docs/knowledge-sync).",
    )
    args = parser.parse_args(argv)

    apply = bool(args.apply)
    mode_label = "APPLY" if apply else "DRY-RUN"
    print(f"[sync-fleet-wiki-knowledge] mode={mode_label}")
    print(f"[sync-fleet-wiki-knowledge] source_root={args.source_root}")
    print(f"[sync-fleet-wiki-knowledge] dest_root={args.dest_root}")

    all_stats: list[SourceStats] = []
    for rel_source, dest_name, stub_filter in _SOURCES:
        source_dir = args.source_root / rel_source
        dest_dir = args.dest_root / dest_name
        stats = _sync_one_source(source_dir, dest_dir, dest_name, stub_filter, apply)
        all_stats.append(stats)

    total_copied = 0
    total_would_copy = 0
    total_unchanged = 0
    total_stub = 0
    total_errors = 0

    print()
    print("[sync-fleet-wiki-knowledge] per-source summary:")
    for stats in all_stats:
        action_count = stats.copied if apply else stats.would_copy
        action_word = "copied" if apply else "would_copy"
        print(
            f"  {stats.label}: {action_word}={action_count} "
            f"stub_skipped={stats.stub_skipped} unchanged={stats.unchanged} "
            f"errors={len(stats.errors)}"
        )
        for err in stats.errors:
            print(f"    ERROR: {err}")
        total_copied += stats.copied
        total_would_copy += stats.would_copy
        total_unchanged += stats.unchanged
        total_stub += stats.stub_skipped
        total_errors += len(stats.errors)

    print()
    if apply:
        print(f"[sync-fleet-wiki-knowledge] TOTAL copied={total_copied}")
    else:
        print(f"[sync-fleet-wiki-knowledge] TOTAL would_copy={total_would_copy} (dry-run — nothing written)")
    print(f"[sync-fleet-wiki-knowledge] TOTAL stub_skipped={total_stub}")
    print(f"[sync-fleet-wiki-knowledge] TOTAL unchanged={total_unchanged}")
    print(f"[sync-fleet-wiki-knowledge] TOTAL errors={total_errors}")

    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
