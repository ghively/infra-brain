"""Resource ownership / on-call / criticality metadata (GitLab issue #116).

Confirmed real gap: no `owner`/`on_call`/`criticality`/`business_unit`/
`responsible_team` field exists on `Resource` or anywhere generically
applicable across all ORM models. Today there is no way to answer "who do I
page when this resource drifts" or "is this a Tier-1 production asset"
without it being buried unstructured in the free-form `Resource.metadata_`
JSONB blob (if it's there at all).

This table is a small, UI-authored overlay joined to `resources.id` (a real
FK, unlike `HostPurposeMap`'s hostname-string key — there is no curated
source-of-truth file for this data the way `host_purpose_map.yml` exists for
host purpose/VLAN, so this starts as pure dashboard-authored metadata with no
GitLab-sync loader). Mirrors the `HostPurposeMap` overlay-table pattern
(host_purpose.py) requested directly in the issue.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class ResourceOwnership(Base):
    """One row per resource's ownership/on-call/criticality overlay entry."""

    __tablename__ = "resource_ownership"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # ``ondelete="CASCADE"`` matches every other resource_id FK in the sibling
    # overlay tables (host_posture.py). Without it Postgres defaults to NO
    # ACTION, so a hard delete of a Resource row would fail with an FK
    # violation on *this* table alone while cascading cleanly off its siblings.
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    owner_team: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    on_call_rotation: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    criticality_tier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # "ui:<username>" for a human dashboard edit — no repo-sync source exists
    # for this table (unlike HostPurposeMap's "<project_id>:<file>"), so this
    # is either empty (never written) or always "ui:<username>".
    source: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_resource_ownership_resource_id"),
    )


__all__ = ["ResourceOwnership"]
