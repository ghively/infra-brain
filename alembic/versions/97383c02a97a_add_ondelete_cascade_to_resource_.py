"""add ondelete cascade to resource_ownership resource_id fk

Revision ID: 97383c02a97a
Revises: 7aae7d4d33eb
Create Date: 2026-08-06 20:43:22.492035

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '97383c02a97a'
down_revision: Union[str, Sequence[str], None] = '7aae7d4d33eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate emitted an unnamed create_foreign_key(None, ...), which
    # would leave downgrade()'s matching drop_constraint(None, ...) unable to
    # reference it (no naming_convention is configured on this metadata, so
    # Alembic can't resolve None to a real name at downgrade time). Both
    # sides are named explicitly, reusing the FK's original name so the
    # constraint identity doesn't change, only its ON DELETE behavior.
    op.drop_constraint(
        "resource_ownership_resource_id_fkey", "resource_ownership", type_="foreignkey"
    )
    op.create_foreign_key(
        "resource_ownership_resource_id_fkey",
        "resource_ownership",
        "resources",
        ["resource_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "resource_ownership_resource_id_fkey", "resource_ownership", type_="foreignkey"
    )
    op.create_foreign_key(
        "resource_ownership_resource_id_fkey",
        "resource_ownership",
        "resources",
        ["resource_id"],
        ["id"],
    )
