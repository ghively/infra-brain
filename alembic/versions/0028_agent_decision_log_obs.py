"""Add token_count + parent_run_id to agent_decision_log (OB-6).

Revision ID: a76a9b9fa04d
Revises: d0e1f2a3b4c5
Create Date: 2026-07-07

MR-G / OB-6: enrich the per-iteration LLM reasoning log with observability
fields so it is queryable for cost/latency and can be correlated into a
LangSmith run tree:

  * ``token_count``   — total tokens for the model turn (from AIMessage
    ``usage_metadata``), nullable (older rows + turns without usage metadata
    stay NULL).
  * ``parent_run_id`` — the framework-provided parent run id when present,
    nullable.

Safety checklist:
  ✅ Two nullable columns added — no NOT NULL added to an existing table
  ✅ No DROP TABLE / DROP COLUMN on existing data
  ✅ Idempotent (existence-checked before adding)
  ✅ Downgrade drops only the additive columns (safe)

Merge-time reconciliation (done): originally authored in parallel with MR-F's
0027 migration, chained onto 0026 with a placeholder revision id that
collided byte-for-byte with MR-F's independently-chosen id ("d0e1f2a3b4c5") —
two different migrations claiming the same revision. Repointed as revision
0028, now correctly chained onto MR-F's real 0027 (down_revision =
d0e1f2a3b4c5), with a freshly generated, unique revision id.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "a76a9b9fa04d"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

_TABLE = "agent_decision_log"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE not in set(inspector.get_table_names()):
        return

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "token_count" not in existing_cols:
        op.add_column(_TABLE, sa.Column("token_count", sa.Integer(), nullable=True))
    if "parent_run_id" not in existing_cols:
        op.add_column(_TABLE, sa.Column("parent_run_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE not in set(inspector.get_table_names()):
        return

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "parent_run_id" in existing_cols:
        op.drop_column(_TABLE, "parent_run_id")
    if "token_count" in existing_cols:
        op.drop_column(_TABLE, "token_count")
