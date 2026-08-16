"""SaaS / API-key inventory model (GitLab #103).

Bucket: SaaSApplication, SaaSApiKeyMetadata.

**METADATA-ONLY (hard safety rule):** this bucket exists to give shadow-IT /
API-key-sprawl visibility, the same "metadata only, never fetch secret
values" bar as the secrets-manager candidate (#98). ``SaaSApiKeyMetadata``
deliberately carries NO column that could hold a key value/secret — only
name/scope/created_at/last_used_at/owner. The key VALUE must never be read
into a row dict, logged, or persisted anywhere in this codebase. See
``tools/saas_inventory_tool.py`` (strips any secret-shaped field at the tool
boundary) and ``agents/saas_inventory.py`` (builds each row from an explicit
metadata-only allowlist).
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONB, _now, _uuid


class SaaSApplication(Base):
    """A third-party SaaS application known to the fleet.

    ``resource_id`` links to the generic ``Resource`` row (type
    ``"saas_application"``) that ``SaaSInventoryAgent.collect()`` upserts.
    ``owner_team`` is the best-known owning team/project name, used by
    ``_emit_saas_edges`` to resolve the ``USES_SAAS_APP`` graph edge —
    best-effort only, never fabricated when unknown.
    """

    __tablename__ = "saas_applications"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[str] = mapped_column(String(256), unique=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    owner_team: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SaaSApiKeyMetadata(Base):
    """One API key's METADATA for a SaaS application — never the key value.

    Keyed on ``(app_name, key_name)``. No column here may ever hold the key
    value/secret — see the module docstring; this is structurally enforced
    (there is no such column to write to) and additionally covered by
    ``tests/db/test_saas_inventory_models.py``.
    """

    __tablename__ = "saas_api_key_metadata"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    app_name: Mapped[str] = mapped_column(String(256))
    key_name: Mapped[str] = mapped_column(String(256))
    scope: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("app_name", "key_name", name="uq_saas_api_key_app_key"),)
