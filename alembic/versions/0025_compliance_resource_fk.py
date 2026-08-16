"""Add nullable resource_id FK to compliance_violations + backfill by host name.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-30

Task 4.5 (Phase 4) converts the highest-risk non-FK string link —
``compliance_violations.host`` (a bare hostname string that logically points
at a ``resources`` row but has no FK) — into a real (soft) nullable
``resource_id`` FK. ``host`` itself is KEPT (display + rows that never match
a resource) and the existing ``uq_compliance_rule_host_status`` unique
constraint is UNCHANGED — this is purely additive.

Mirrors the 0021_repair_r7_resource_id_drift.py / R7Asset.resource_id pattern:
nullable FK column + index, inspector-guarded so this is idempotent and safe
to run against a DB that already has the column (no-op) or one that doesn't
yet (applies the DDL).

Backfill: a correlated UPDATE that links each ``compliance_violations`` row
with ``resource_id IS NULL`` to the ``resources`` row whose ``name`` exactly
matches ``host`` — ONLY when that name is unambiguous (matches exactly one
resource). Ambiguous host names (matching more than one resource — e.g. the
same hostname recorded under two domains) are deliberately left NULL rather
than guessing. Implemented as a scalar correlated subquery guarded by a
``(SELECT COUNT(*) ... ) = 1`` predicate, which is valid identical SQL on both
SQLite (test target) and PostgreSQL (live target) — no dialect-specific
syntax (e.g. no UPDATE...FROM) is used.

Safety checklist:
  All columns nullable — no NOT NULL added to any table.
  No DROP TABLE / DROP COLUMN on existing tables in upgrade().
  Every add guarded by an inspector existence check (idempotent / re-runnable).
  ondelete="SET NULL" — deleting a Resource never cascades a violation delete.
  Backfill UPDATE only touches rows with resource_id IS NULL and an
    unambiguous name match; safe to re-run (no-op on rows already linked).
  Downgrade drops the index/FK/column, guarded by inspector checks.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID

# revision identifiers, used by Alembic.
revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

# Cross-dialect UUID: PostgreSQL UUID on the live DB, CHAR(32) elsewhere —
# mirrors resources.id PK type so the FK column matches (0010 uses sa.Uuid()).
_UUID = sa.Uuid().with_variant(_PG_UUID(as_uuid=True), "postgresql")

_TABLE = "compliance_violations"
_FK_NAME = "fk_compliance_violation_resource"
_IX_NAME = "ix_compliance_violations_resource_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())

    if _TABLE not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "resource_id" not in cols:
        op.add_column(_TABLE, sa.Column("resource_id", _UUID, nullable=True))
        # Refresh column set post-add for the backfill guard below.
        cols.add("resource_id")

    if not is_sqlite:
        fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
        if _FK_NAME not in fks:
            op.create_foreign_key(
                _FK_NAME,
                _TABLE,
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="SET NULL",
            )

    idxs = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _IX_NAME not in idxs:
        op.create_index(_IX_NAME, _TABLE, ["resource_id"])

    # Backfill: link unlinked violations to the ONE matching resource by name.
    # Ambiguous (>1 match) or unmatched (0 match) hosts are left NULL.
    if "resources" in tables:
        op.execute(
            sa.text(
                f"""
                UPDATE {_TABLE}
                SET resource_id = (
                    SELECT r.id FROM resources r WHERE r.name = {_TABLE}.host
                )
                WHERE resource_id IS NULL
                  AND host != ''
                  AND (SELECT COUNT(*) FROM resources r WHERE r.name = {_TABLE}.host) = 1
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())

    if _TABLE not in tables:
        return

    idxs = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _IX_NAME in idxs:
        op.drop_index(_IX_NAME, table_name=_TABLE)

    if not is_sqlite:
        fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
        if _FK_NAME in fks:
            op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "resource_id" in cols:
        op.drop_column(_TABLE, "resource_id")
