"""add software_licenses licensing table

Revision ID: bb2c145b8f38
Revises: 9d83fb71e295
Create Date: 2026-07-30 09:59:51.847858

Adds ``software_licenses`` (db/models/licensing.py's ``SoftwareLicense`` —
GitLab issue #97 license-compliance reconciliation entitlement rows,
consumed by ``agents/licensing.py``'s ``LicensingAgent``).

Existence-guarded (mirrors 0020_windows_services_software_cischedule.py's
"✅ Uses op.create_table() DDL — NOT Base.metadata.create_all()" pattern):
migrations 0002/0014 call ``Base.metadata.create_all(bind=op.get_bind())``
with NO table filter, against the CURRENT (not frozen-at-that-revision)
``Base.metadata`` — so on a from-empty DB those early migrations already
pre-create ``software_licenses`` (it's now part of the live model) before
this migration runs. Confirmed by reproducing a from-empty
``alembic upgrade head`` locally: an unguarded ``op.create_table`` here hit
``DuplicateTable``. Every other "add table" migration since 0002/0014 carries
the same guard for the same reason (see 65f1043a2504/7af94c999f92/
808138e3c101/9483820d6f52/eeda3039b7b1's "Guard: migrations 0002/0003/0014
call Base.metadata.create_all()..." comments).

Safety checklist:
  ✅ Only CREATE TABLE — no ALTER to existing tables
  ✅ Downgrade is inspector-guarded (drop table only if it exists)
  ✅ Existence-guarded CREATE — safe against the 0002/0014 blanket create_all
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb2c145b8f38'
down_revision: Union[str, Sequence[str], None] = '9d83fb71e295'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "software_licenses" not in existing_tables:
        op.create_table(
            'software_licenses',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('product', sa.String(length=256), nullable=False),
            sa.Column('license_type', sa.String(length=64), nullable=False),
            sa.Column('vendor', sa.String(length=256), nullable=True),
            sa.Column('seats_entitled', sa.Integer(), nullable=True),
            sa.Column('cores_entitled', sa.Integer(), nullable=True),
            sa.Column('expiry', sa.DateTime(timezone=True), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('source', sa.String(length=256), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('product', 'license_type', name='uq_software_license_product_type'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "software_licenses" in set(inspector.get_table_names()):
        op.drop_table('software_licenses')
