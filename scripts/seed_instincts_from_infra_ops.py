"""
One-time import: infra-ops YAML instinct ledger -> infra-brain Instinct table.

TRK-276 / GitLab #154: the maintainer decided infra-brain's DB-backed instinct store is
authoritative; the infra-ops git-committed YAML ledger is imported into it,
not synced live. This script is that one-time (repeatable, idempotent)
import — it is NOT a scheduled job and is not wired into scheduler.py.

Usage:
    python scripts/seed_instincts_from_infra_ops.py [--dry-run] [--zone corpor]

Requires INFRA_OPS_ROOT (path to a local infra-ops checkout) in the
environment, or pass --infra-ops-root explicitly. Reads every *.yml file
under <root>/knowledge/instincts/<domain>/. The database connection comes from
infra-brain's own Settings (POSTGRES_URL / config.py), same as the rest of
the app — this script does not open its own ad hoc connection.

Writes go through the Instinct ORM model (db/models/core.py), never raw SQL.

Dedup: content-based, on (zone, domain, pattern) — NOT the DB primary key.
Instinct.id is always a freshly generated UUID, so "INSERT ... ON CONFLICT DO
NOTHING" targeting the primary key never actually conflicts; running an
insert-with-fresh-uuid script twice silently duplicates every row. Querying
for an existing row with the same (zone, domain, pattern) before inserting is
what makes this script safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.7

# The corporate Brain DB lives in the corporate zone. HSA ("in-zone") instincts
# must NEVER cross into it (trust boundary: corporate and HSA knowledge never
# auto-cross — SPEC.md §2). infra-ops dropped the zone directory level, so the
# ledger no longer carries zone information in its layout: --zone is now the
# zone *label* the import runs under, defaulting to corpor. The only remaining
# zone signal in the ledger is a file's own optional `zone` field, enforced by
# the defensive gate in load_ledger_entries().
DEFAULT_ZONE = "corpor"


def _require_infra_ops_root() -> Path:
    """Read INFRA_OPS_ROOT from the environment; fail loudly rather than fall
    back to a real, environment-specific absolute path. This script has no
    meaningful default location for a sibling checkout of infra-ops, so the
    caller must set it (or pass --infra-ops-root)."""
    root = os.getenv("INFRA_OPS_ROOT")
    if not root:
        raise SystemExit(
            "INFRA_OPS_ROOT must be set (or pass --infra-ops-root) — refusing "
            "to fall back to a hardcoded path"
        )
    return Path(root)


def _parse_dt(value: Any) -> datetime:
    if not value:
        return datetime.now(tz=UTC)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(tz=UTC)


def load_ledger_entries(instincts_root: Path, zone: str = DEFAULT_ZONE) -> list[dict]:
    """Parse every ``*.yml`` file under ``instincts_root/<domain>/`` into import-ready dicts.

    The ledger is flat — ``knowledge/instincts/<domain>/*.yml``, with no zone
    directory level — so ``zone`` is the label the import runs under, not a
    subtree selector.

    Deliberately tolerant of the ledger's real on-disk schema, confirmed by
    reading the actual files rather than assumed: required fields are
    ``claim`` (legacy files may use ``pattern`` instead), ``domain``, and
    ``confidence``. Files also carry ``evidence_summary``, ``evidence`` lists,
    ``status``, ``proposed_by``/``proposed_at``, ``governance_ledger_ref``,
    ``source_proposal``, and free-form domain-specific nested maps (e.g.
    ``separation_model``, ``hsm_vendor_details``, ``vlan_topology``) — those
    are read without error but not persisted, since the Instinct table has no
    columns for them (zone, domain, pattern, confidence, promoted_by,
    promoted_at, citation only).
    """
    rows: list[dict] = []
    if not instincts_root.exists():
        log.info("Instincts ledger %s does not exist — nothing to import.", instincts_root)
        return rows

    for yml_path in sorted(instincts_root.glob("*/*.yml")):
        try:
            data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
            log.warning("SKIP %s: parse error — %s", yml_path.name, exc)
            continue

        if not isinstance(data, dict):
            log.warning("SKIP %s: not a YAML mapping", yml_path.name)
            continue

        # Defensive zone gate: with the zone directory level gone, a file's own
        # ``zone`` field is the last zone signal in the ledger. Refuse any file
        # whose declared zone disagrees (e.g. a mis-filed HSA instinct); files
        # that declare none inherit the target zone. Never cross zones.
        file_zone = str(data.get("zone", zone))
        if file_zone != zone:
            log.warning(
                "SKIP %s: declared zone %r != target zone %r (zone separation)",
                yml_path.name,
                file_zone,
                zone,
            )
            continue

        pattern = data.get("claim") or data.get("pattern")
        if not pattern:
            log.warning("SKIP %s: no claim/pattern field", yml_path.name)
            continue

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            log.warning("SKIP %s: unparseable confidence %r", yml_path.name, data.get("confidence"))
            continue

        rows.append(
            {
                "source_path": str(yml_path.relative_to(instincts_root.parent.parent))
                if instincts_root.parent.parent in yml_path.parents
                else str(yml_path),
                "source_id": str(data.get("id") or yml_path.stem),
                "zone": file_zone[:32],
                "domain": str(data.get("domain", "common"))[:64],
                "pattern": str(pattern).strip(),
                "confidence": confidence,
                "promoted_by": str(data.get("promoted_by") or "infra-ops-ledger-import")[:128],
                "promoted_at": _parse_dt(data.get("promoted_at") or data.get("proposed_at")),
                "citation": data.get("citation"),
            }
        )
    return rows


def import_instincts(
    session: Session,
    instincts_root: Path,
    zone: str = DEFAULT_ZONE,
    min_confidence: float = MIN_CONFIDENCE,
    dry_run: bool = False,
) -> dict[str, list[dict]]:
    """Import ledger entries into the ``instincts`` table via the Instinct ORM model.

    Returns a dict with ``inserted`` / ``skipped_duplicate`` /
    ``skipped_low_confidence`` lists of the source entry dicts, so callers
    (CLI or tests) can report or assert on exactly what happened.
    """
    from infra_brain.db.models import Instinct

    entries = load_ledger_entries(instincts_root, zone=zone)
    inserted: list[dict] = []
    skipped_duplicate: list[dict] = []
    skipped_low_confidence: list[dict] = []

    for entry in entries:
        if entry["confidence"] < min_confidence:
            skipped_low_confidence.append(entry)
            continue

        # Content-based dedup — see module docstring for why this can't be a
        # DB-level ON CONFLICT on the primary key.
        existing = (
            session.query(Instinct)
            .filter(
                Instinct.zone == entry["zone"],
                Instinct.domain == entry["domain"],
                Instinct.pattern == entry["pattern"],
            )
            .first()
        )
        if existing is not None:
            skipped_duplicate.append(entry)
            continue

        if not dry_run:
            session.add(
                Instinct(
                    zone=entry["zone"],
                    domain=entry["domain"],
                    pattern=entry["pattern"],
                    confidence=entry["confidence"],
                    promoted_by=entry["promoted_by"],
                    promoted_at=entry["promoted_at"],
                    citation=entry["citation"],
                )
            )
        inserted.append(entry)

    if not dry_run and inserted:
        session.commit()

    return {
        "inserted": inserted,
        "skipped_duplicate": skipped_duplicate,
        "skipped_low_confidence": skipped_low_confidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be imported without writing"
    )
    parser.add_argument(
        "--zone",
        default=DEFAULT_ZONE,
        help=f"Zone label to import the flat ledger under (default: {DEFAULT_ZONE}). "
        "The corporate Brain DB must only receive corporate-zone instincts.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=MIN_CONFIDENCE,
        help=f"Skip ledger entries below this confidence (default: {MIN_CONFIDENCE})",
    )
    parser.add_argument(
        "--infra-ops-root",
        default=None,
        help="Path to a local infra-ops checkout (overrides INFRA_OPS_ROOT env var)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    root = Path(args.infra_ops_root) if args.infra_ops_root else _require_infra_ops_root()
    instincts_root = root / "knowledge" / "instincts"

    from infra_brain.db.session import get_session

    with get_session() as session:
        result = import_instincts(
            session,
            instincts_root,
            zone=args.zone,
            min_confidence=args.min_confidence,
            dry_run=args.dry_run,
        )

    verb = "WOULD INSERT" if args.dry_run else "INSERTED"
    for e in result["inserted"]:
        log.info(
            "  %s: [%s/%s] %s (confidence=%s)",
            verb,
            e["zone"],
            e["domain"],
            e["source_id"],
            e["confidence"],
        )
    for e in result["skipped_duplicate"]:
        log.info("  SKIPPED (duplicate): [%s/%s] %s", e["zone"], e["domain"], e["source_id"])
    for e in result["skipped_low_confidence"]:
        log.info(
            "  SKIPPED (confidence %.2f < %.2f): [%s/%s] %s",
            e["confidence"],
            args.min_confidence,
            e["zone"],
            e["domain"],
            e["source_id"],
        )

    if args.dry_run:
        log.info(
            "\nDry-run. Would insert: %d, would skip (duplicate): %d, would skip (low confidence): %d",
            len(result["inserted"]),
            len(result["skipped_duplicate"]),
            len(result["skipped_low_confidence"]),
        )
    else:
        log.info(
            "\nDone. Inserted: %d, Skipped (duplicate): %d, Skipped (low confidence): %d",
            len(result["inserted"]),
            len(result["skipped_duplicate"]),
            len(result["skipped_low_confidence"]),
        )


if __name__ == "__main__":
    main()
