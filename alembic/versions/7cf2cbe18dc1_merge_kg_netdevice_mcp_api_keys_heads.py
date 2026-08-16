"""merge KG NetDevice + MCP api-keys heads

Revision ID: 7cf2cbe18dc1
Revises: 993ba2d34e88, 0035_mcp_api_keys
Create Date: 2026-07-24 00:07:09.433640

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "7cf2cbe18dc1"
down_revision: Union[str, Sequence[str], None] = ("993ba2d34e88", "0035_mcp_api_keys")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
