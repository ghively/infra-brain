"""Add custom_views table for user-authored OpenUI views.

Revision ID: 0014_add_custom_views
Revises: 0013_octopus_deep
Create Date: 2026-06-23

Adds the custom_views table (Phase 5, T5.2):
  * NEW table: custom_views
    - id: UUID primary key
    - user_id: nullable FK → ui_users.id
    - title: String(256), not null
    - prompt: Text, not null
    - openui_lang: Text, not null
    - share_token: String(32), unique, not null
    - is_public: Boolean, default false
    - created_at: timestamptz, default now()
  * Indexes: ix_custom_views_share_token, ix_custom_views_user_id

Uses Base.metadata.create_all (idempotent, dialect-agnostic) to stay
consistent with migrations 0001/0002/0003/0005 and to remain SQLite-
compatible for the test_migration.py suite.

Safety checklist:
  ✅ New table only — no NOT NULL added to existing tables
  ✅ No DROP TABLE or DROP COLUMN on existing tables
  ✅ No CONCURRENTLY concern — new table, no concurrent traffic
  ✅ No lock_timeout concern — new table
  ✅ Downgrade present
"""

from alembic import op
from infra_brain.db.models import Base

revision = "0014_add_custom_views"
down_revision = "0013_octopus_deep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_all is idempotent (checkfirst=True) and dialect-agnostic, but it
    # must be scoped to the ONE table this revision owns. Base.metadata is the
    # LIVE ORM metadata, so an unscoped create_all() here creates every table
    # the models will ever have — including ones added by later revisions,
    # which then fail with DuplicateTable when the chain reaches them on a
    # fresh database (TRK-312; see 0003 for the same defect and the full
    # write-up). Scoped to custom_views, mirroring downgrade() below.
    from infra_brain.db.models import CustomView

    Base.metadata.create_all(bind=op.get_bind(), tables=[CustomView.__table__])


def downgrade() -> None:
    from infra_brain.db.models import CustomView

    CustomView.__table__.drop(bind=op.get_bind(), checkfirst=True)
