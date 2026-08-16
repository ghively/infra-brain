"""One-off cleanup for F-018: iac `resources` rows duplicated by name.

For every iac name with >1 row: keep the newest row (max last_seen),
re-point child FKs (snapshots, resource_configs, drift_events,
resource_relationships) at the survivor, then delete the older row(s).

READ FIRST: run with --dry-run (default) to see what would change; pass
--apply to actually write. Prints a per-name report either way.
Usage:  python scripts/dedupe_iac_resources.py [--apply]
"""

import sys

from sqlalchemy import func

from infra_brain.db.models import (
    DriftEvent,
    Resource,
    ResourceConfig,
    ResourceRelationship,
    Snapshot,
)
from infra_brain.db.session import get_session


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[dedupe-iac] mode={mode}")
    merged = 0
    with get_session() as session:
        dup_names = [
            row[0]
            for row in (
                session.query(Resource.name)
                .filter(Resource.domain == "iac")
                .group_by(Resource.name)
                .having(func.count(Resource.id) > 1)
                .all()
            )
        ]
        print(f"[dedupe-iac] duplicate names found: {len(dup_names)}")
        for name in dup_names:
            rows = (
                session.query(Resource)
                .filter_by(domain="iac", name=name)
                .order_by(Resource.last_seen.desc())
                .all()
            )
            keep, dupes = rows[0], rows[1:]
            print(f"  {name!r}: keeping {keep.id} (type={keep.type}), merging {len(dupes)}")
            for dup in dupes:
                session.query(Snapshot).filter_by(resource_id=dup.id).update(
                    {"resource_id": keep.id}, synchronize_session=False
                )
                session.query(ResourceConfig).filter_by(resource_id=dup.id).update(
                    {"resource_id": keep.id}, synchronize_session=False
                )
                session.query(DriftEvent).filter_by(resource_id=dup.id).update(
                    {"resource_id": keep.id}, synchronize_session=False
                )
                session.query(ResourceRelationship).filter_by(from_resource_id=dup.id).update(
                    {"from_resource_id": keep.id}, synchronize_session=False
                )
                session.query(ResourceRelationship).filter_by(to_resource_id=dup.id).update(
                    {"to_resource_id": keep.id}, synchronize_session=False
                )
                session.delete(dup)
                merged += 1
        if apply:
            session.commit()
            print(f"[dedupe-iac] committed — {merged} duplicate rows merged")
        else:
            session.rollback()
            print(f"[dedupe-iac] dry-run — would merge {merged} rows (rolled back)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
