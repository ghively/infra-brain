"""IdP identity / RBAC audit models (GitLab issue #102).

Bucket: IdentityPrincipal, IdentityGroupMembership.

Design notes (per the issue's own data-shape sketch):
- Distinct from the existing per-system access tables (VspherePermission's
  ``GRANTED_ON`` edges, OctopusTeam's ``HAS_ACCESS`` membership) — those
  describe "this per-system account has this grant"; these tables describe
  "this canonical IdP identity IS a person/service-account", independent of
  any one target system. ``IdentityAgent`` links the two via the
  ``IS_PRINCIPAL_FOR`` graph edge (RelationshipType), pointing from a row here
  at the already-existing ``identity``/``user_account`` convergence Resource
  nodes (built by graph_maintenance._populate_convergence_nodes from
  per-system usernames) — never by adding a new per-system table.
- ``source`` + ``external_id`` (the IdP's own user/group id) is the natural
  upsert key, mirroring R7Asset/OctopusUser's "upstream id is the natural
  key" convention elsewhere in this codebase.
- IdentityGroupMembership is a thin join row (principal -> IdP group), not a
  new group entity table — the group's own metadata (name, id) is denormalized
  onto the membership row directly, matching OctopusTeam's
  ``member_user_ids`` JSON-array approach's spirit but as first-class rows so
  each membership can be upserted/pruned independently rather than diffing a
  JSON blob.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import JSONB, Base, _now, _uuid


class IdentityPrincipal(Base):
    """One canonical person/service-account, as known to the source IdP.

    Natural key: (source, external_id) — e.g. ("okta", "00u1a2b3c4d5e6f7g8h9").
    """

    __tablename__ = "identity_principals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(32))  # "okta" | "azure_ad" | "ldap" | ...
    external_id: Mapped[str] = mapped_column(String(256))
    username: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="")  # ACTIVE|SUSPENDED|DEPROVISIONED|...
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_identity_principal_natkey"),
        Index("ix_identity_principals_email", "email"),
        Index("ix_identity_principals_username", "username"),
    )


class IdentityGroupMembership(Base):
    """One (IdP principal) -> (IdP group) membership row.

    Natural key: (principal_id, source, group_external_id) — a principal can
    belong to many groups; each membership is upserted/pruned independently.
    """

    __tablename__ = "identity_group_memberships"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_principals.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(32))  # matches IdentityPrincipal.source
    group_external_id: Mapped[str] = mapped_column(String(256))
    group_name: Mapped[str] = mapped_column(String(256), default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "source",
            "group_external_id",
            name="uq_identity_group_membership_natkey",
        ),
        Index("ix_identity_group_memberships_principal_id", "principal_id"),
    )


__all__ = ["IdentityGroupMembership", "IdentityPrincipal"]
