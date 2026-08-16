"""add netdevice_resource_id to host_identities (KG-4)

Revision ID: 993ba2d34e88
Revises: b4d40e450bb8
Create Date: 2026-07-23

Adds ``host_identities.netdevice_resource_id`` — a nullable FK to
``resources.id`` — the ninth per-source column on HostIdentity, wiring
``NetDevice`` (SNMP-discovered switch/router inventory) into identity
reconciliation. Confirmed during the 2026-07-23 KG audit as the only
cloud/net-ish domain table omitted from ``_SOURCE_KEYS``.

Nullable, no ``server_default`` — matches every sibling ``*_resource_id``
column, so no backfill is needed and this stays clear of CLAUDE.md's
"NOT NULL without default" danger pattern.

Guarded with inspector existence checks, following the established idiom
from 0030_kg_enrichment.py (which added the net/cloud/k8s FK columns to
this same table) and 0033_collection_run_sweep_id.py: 0001_initial_schema.py
pre-creates the existing ``host_identities`` table via a restricted
``Base.metadata.create_all()`` from the CURRENT live model — which already
includes ``netdevice_resource_id`` — so an unguarded ``add_column`` would
fail with "duplicate column" whenever the schema is built via create_all()
first (test_migration_parity.py / test_migration_zone.py) and the migration
chain is then replayed. ``batch_alter_table`` keeps the ADD COLUMN portable
to SQLite.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "993ba2d34e88"
down_revision = "b4d40e450bb8"
branch_labels = None
depends_on = None

_TABLE = "host_identities"
_COLUMN = "netdevice_resource_id"


def _cols(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if _TABLE in tables and _COLUMN not in _cols(inspector, _TABLE):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(_COLUMN, sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if _TABLE in tables and _COLUMN in _cols(inspector, _TABLE):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
