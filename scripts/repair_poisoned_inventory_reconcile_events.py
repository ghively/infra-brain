"""Repair for H-6: InventoryReconcileEvent rows poisoned before the fix in
``agents/inventory_reconcile.py`` (see that file's ``_reconcile_main_inventory``).

Before the fix, an MR-creation failure (swallowed) or MR creation being
disabled entirely (``INFRA_BRAIN_MR_ENABLED`` unset) still resulted in an
``InventoryReconcileEvent`` row written with ``status="proposed"`` and
``mr_url=None`` — i.e. claiming a proposal existed when none did.
``InventoryReconcileAgent._filter_already_proposed()`` excludes any host with
an existing ``status="proposed"`` row FOREVER, so every host poisoned this
way silently stopped being reconciled, permanently, with no error ever
surfaced.

This script finds exactly those poisoned rows — ``status="proposed"`` AND
``mr_url IS NULL`` — and flips them to ``status="failed"``, a status
``_filter_already_proposed()`` does not exclude, so the host is picked up and
correctly retried on the very next scheduled run.

Nothing is ever deleted (same convention as
``scripts/flag_stale_wrong_direction_proposals.py``): the row's history
(when it was first detected, what group it targeted) is preserved, and it is
obvious on inspection why the row was repaired.

A genuinely successful proposal always has ``mr_url`` set, so this predicate
cannot match a real, healthy "proposed" row — the repair is safe. It is also
idempotent: a row already flipped to "failed" no longer matches
``status="proposed"``, so re-running finds nothing to do.

READ FIRST: run with --dry-run (default) to see what would change; pass
--apply to actually write. Prints a per-row report either way.

Usage:  python scripts/repair_poisoned_inventory_reconcile_events.py [--apply]
"""

from __future__ import annotations

import sys

from infra_brain.db.models import InventoryReconcileEvent
from infra_brain.db.session import get_session


def _report_row(event: InventoryReconcileEvent) -> None:
    print(
        f"  {event.id} host={event.host!r} domain={event.domain!r} "
        f"target_group={event.target_group!r} detected_at={event.detected_at}"
    )


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[repair-poisoned-inventory-reconcile] mode={mode}")

    repaired = 0
    with get_session() as session:
        candidates = (
            session.query(InventoryReconcileEvent)
            .filter(
                InventoryReconcileEvent.status == "proposed",
                InventoryReconcileEvent.mr_url.is_(None),
            )
            .order_by(InventoryReconcileEvent.detected_at.asc())
        )

        for event in candidates:
            _report_row(event)
            repaired += 1
            if apply:
                event.status = "failed"

        print(f"[repair-poisoned-inventory-reconcile] matched rows: {repaired}")

        if apply:
            session.commit()
            print(
                f"[repair-poisoned-inventory-reconcile] committed — {repaired} row(s) "
                'flipped to status="failed" (will be retried on the next run)'
            )
        else:
            session.rollback()
            print(
                f"[repair-poisoned-inventory-reconcile] dry-run — would repair "
                f"{repaired} row(s) (rolled back)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
