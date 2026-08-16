"""One-off backlog cleanup for TRK-191 / GitLab #123.

GraphMaintenanceAgent's own "graph-health" summary Resource (domain=
"graph_maintenance") used to flow through the generic Resource/Snapshot
pipeline every run, so DriftAgent (intentionally domain-agnostic) diffed its
ever-changing internal stats (timings, typed-edge counts, ...) as if they were
real fleet drift — 56,988 open DriftEvent rows, roughly a third of the
open-drift queue, none of it real fleet noise.

agents/graph_maintenance.py's collect() has been fixed to stop writing that
Snapshot at all (see the TRK-191 comments there), which stops new noise from
being created. This script is the one-time backlog cleanup for the rows that
already accumulated: it marks every OPEN DriftEvent whose Resource.domain ==
"graph_maintenance" as resolved. Rows are never deleted — this repo's
convention is a soft status change (see dedupe_iac_resources.py for the
analogous pattern with a --dry-run/--apply split), so the events remain
available for audit/history.

READ FIRST: run with --dry-run (default) to see the count that would change;
pass --apply to actually write.
Usage:  python scripts/close_graph_maintenance_drift_backlog.py [--apply]
"""

import sys

from infra_brain.db.models import DriftEvent, Resource
from infra_brain.db.session import get_session


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[close-graph-maintenance-drift-backlog] mode={mode}")

    with get_session() as session:
        query = (
            session.query(DriftEvent)
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.status == "open")
            .filter(Resource.domain == "graph_maintenance")
        )
        matched = query.count()
        print(f"[close-graph-maintenance-drift-backlog] open graph_maintenance drift events: {matched}")

        if matched and apply:
            updated = query.update({"status": "resolved"}, synchronize_session=False)
            session.commit()
            print(f"[close-graph-maintenance-drift-backlog] committed — {updated} rows marked resolved")
        elif matched:
            session.rollback()
            print(
                f"[close-graph-maintenance-drift-backlog] dry-run — would mark {matched} rows "
                "resolved (rolled back)"
            )
        else:
            session.rollback()
            print("[close-graph-maintenance-drift-backlog] nothing to do")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
