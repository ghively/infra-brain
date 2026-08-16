"""backfill stale collection_runs drift_count

Revision ID: 74cd24313ffd
Revises: 005f5781df29
Create Date: 2026-08-11

DATA REPAIR, no schema change. Companion to the code fix that made
``ETLConnector.run()`` persist its post-detail-phase drift recount instead of
updating only the in-memory result.

Why this exists: ``linux`` and ``windows`` write every one of their
``DriftEvent`` rows in the DETAIL phase, i.e. after ``run()`` has already
computed and stored ``CollectionRun.drift_count``. The recount that followed
updated ``result.drift_count`` (the returned object) but never the stored row,
so for those two collectors the persisted column has always been 0 — while
``drift_events`` held the real count. Live evidence at the time of writing:
a ``linux`` run returned 14 and wrote 14 ``drift_events`` rows while its
``collection_runs`` row said 0; historically, 2026-08-02 12:00 UTC recorded 6
real drift events and reported 0. **87 rows** in the live database disagreed
with reality. Run-list readers show that column, so linux/windows drift was
invisible there.

The code fix only affects FUTURE runs — existing rows do not self-correct.

Safety properties:
  - Derived value only. ``drift_count`` is a cached count OF ``drift_events``;
    this sets it to the true count. No source-of-truth data is written,
    nothing is deleted, and no row is created.
  - Idempotent. Re-running is a no-op once counts already agree (the WHERE
    clause matches nothing).
  - Bounded. Touches only rows that actually disagree.
  - Single statement, inside the migration transaction, under the 4s
    ``lock_timeout`` set in ``alembic/env.py``.

``downgrade()`` is intentionally a no-op — see its docstring.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "74cd24313ffd"
down_revision: Union[str, Sequence[str], None] = "005f5781df29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BACKFILL = sa.text(
    """
    UPDATE collection_runs cr
       SET drift_count = (
               SELECT count(*) FROM drift_events de
                WHERE de.collection_run_id = cr.id
           )
     WHERE cr.drift_count <> (
               SELECT count(*) FROM drift_events de
                WHERE de.collection_run_id = cr.id
           )
    """
)


def upgrade() -> None:
    """Set every stale drift_count to the true drift_events count."""
    result = op.get_bind().execute(_BACKFILL)
    # rowcount is informational; surfaced in the migrate container's log so the
    # deploy record shows how many rows were actually repaired.
    print(f"[74cd24313ffd] repaired drift_count on {result.rowcount} collection_runs row(s)")


def downgrade() -> None:
    """Deliberately a no-op.

    The pre-repair state was simply WRONG — a cached count that disagreed with
    the rows it counts. There is no meaningful state to restore, and no record
    of the incorrect values is kept (they carried no information). Re-introducing
    them would mean re-hiding real drift from the run list.
    """
