"""Indexes for the vuln_queue + eol_registry writers (MR3).

Revision ID: 0008_vuln_eol_writer_indexes
Revises: 0007_vsphere_relational
Create Date: 2026-06-24

MR3 turns vuln_queue + eol_registry into self-populating tables:
  * VulnAgent upserts vuln_queue keyed on the natural pair (resource_id, cve_id).
  * EOLAgent + the add_eol_product MCP tool upsert eol_registry keyed on
    asset_name (so manual + auto-derived entries merge).

Both upserts do a per-row SELECT-then-insert/update, so these indexes back the
lookup. (resource_id, cve_id) is UNIQUE — it is a true natural key and a dup
would be a feed bug. asset_name is a plain (non-unique) index: the merge is
application-level and we do not want a hard constraint to reject a pre-existing
duplicate left by the old always-insert add_eol_product behavior.

Guarded with the inspector: 0001 seeds a fresh DB via Base.metadata.create_all,
and re-running must be a no-op (mirrors 0005/0006/0007).

KNOWN RISK (documented, not fixed here — this migration is already applied
everywhere, so rewriting its `upgrade()` would desync it from what's actually
live): neither `create_index` call below uses `postgresql_concurrently=True`.
Both tables are actively upserted by VulnAgent/EOLAgent, so a non-concurrent
index build takes an ACCESS EXCLUSIVE lock for its duration — on a
sufficiently large table, or if this migration ever re-runs while a collection
run is mid-write, that blocks writes for the build's duration. Low risk today
at current table sizes. Future migrations touching these (or any other
actively-written) tables should use `postgresql_concurrently=True` — see the
migration-create skill's CONCURRENTLY guidance.
"""

from alembic import op
from sqlalchemy import inspect

revision = "0008_vuln_eol_writer_indexes"
down_revision = "0007_vsphere_relational"
branch_labels = None
depends_on = None

_VULN_IDX = "ix_vuln_queue_resource_cve"
_EOL_IDX = "ix_eol_registry_asset_name"


def _existing_indexes(inspector, table: str) -> set[str]:
    try:
        return {ix["name"] for ix in inspector.get_indexes(table)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "vuln_queue" in tables:
        if _VULN_IDX not in _existing_indexes(inspector, "vuln_queue"):
            op.create_index(_VULN_IDX, "vuln_queue", ["resource_id", "cve_id"], unique=True)

    if "eol_registry" in tables:
        if _EOL_IDX not in _existing_indexes(inspector, "eol_registry"):
            op.create_index(_EOL_IDX, "eol_registry", ["asset_name"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "eol_registry" in tables and _EOL_IDX in _existing_indexes(inspector, "eol_registry"):
        op.drop_index(_EOL_IDX, table_name="eol_registry")
    if "vuln_queue" in tables and _VULN_IDX in _existing_indexes(inspector, "vuln_queue"):
        op.drop_index(_VULN_IDX, table_name="vuln_queue")
