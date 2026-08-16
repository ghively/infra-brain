"""add incident_key correlation to drift_events and compliance_violations

Revision ID: 2314097fd962
Revises: ec4de4fefbef
Create Date: 2026-07-28 00:00:00.000000

TRK-242 (GitLab #113): native incident-management (PagerDuty/Opsgenie-style)
correlation. Adds a nullable ``incident_key`` column + index to both
``drift_events`` and ``compliance_violations`` — mirrors the existing
``drift_events.jira_key`` correlation column. Populated by
NotificationAgent.notify_incidents() (agents/notification.py) with the same
dedup_key sent to the ops-alert webhook (tools/ops_webhook.py
send_ops_alert), and read back by the ack-in webhook
(POST /webhooks/incident/ack, webhooks.py) to update status on ack/resolve.

Guarded with inspector existence checks (same idempotency pattern as
b4d40e450bb8_add_caller_key_hash_to_agent_action_log.py and
0033_collection_run_sweep_id.py): 0001_initial_schema.py's restricted
create_all() builds both of these tables (which it owns) from the CURRENT
live model, which already includes incident_key — so a plain op.add_column()
here would fail with "duplicate column" whenever the schema is built via
create_all() first (e.g. test_migration_parity.py, test_migration_zone.py)
and the migration chain is then replayed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '2314097fd962'
down_revision: Union[str, Sequence[str], None] = 'ec4de4fefbef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN = 'incident_key'
_TARGETS = (
    ('drift_events', 'ix_drift_events_incident_key'),
    ('compliance_violations', 'ix_compliance_violations_incident_key'),
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    for table, index_name in _TARGETS:
        columns = {c['name'] for c in inspector.get_columns(table)}
        if _COLUMN not in columns:
            op.add_column(table, sa.Column(_COLUMN, sa.String(length=128), nullable=True))
        indexes = {ix['name'] for ix in inspector.get_indexes(table)}
        if index_name not in indexes:
            op.create_index(index_name, table, [_COLUMN])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    for table, index_name in _TARGETS:
        indexes = {ix['name'] for ix in inspector.get_indexes(table)}
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        columns = {c['name'] for c in inspector.get_columns(table)}
        if _COLUMN in columns:
            op.drop_column(table, _COLUMN)
