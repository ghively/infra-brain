"""Container image registry scan results (GitLab issue #101).

Registry-side data only: image identity (registry/repo/tag/digest), best-effort
vulnerability-scan summary, and signing status — read via the registry-agnostic
OCI Distribution API v2 Referrers endpoint (``tools/container_registry_tool.py``).
Deliberately does NOT model running-container placement: the ``HOSTED_BY``
relationship (container -> vm/k8s_node) stays ``[DEFERRED — no container
collector]`` per ``db/relationships.py`` — this issue's own adversarial-review
comment scoped that out, since it needs a separate running-container collector
still blocked on TRK-041/K8s.

Mirrors the ``ResourceOwnership`` overlay-table shape (ownership.py): one row
per ``resources.id``, upserted on the natural key (registry, repo, digest).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import JSONB, Base, _now, _uuid


class ContainerImage(Base):
    """One row per (registry, repo, digest) container image observed by a scan."""

    __tablename__ = "container_images"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id"), nullable=False, index=True
    )

    registry: Mapped[str] = mapped_column(String(512), nullable=False)
    repo: Mapped[str] = mapped_column(String(512), nullable=False)
    tag: Mapped[str] = mapped_column(String(256), nullable=False)
    digest: Mapped[str] = mapped_column(String(128), nullable=False)

    # Best-effort summary derived from the OCI Referrers API (counts/types of
    # attached scan-attestation artifacts) — shape varies by scanner, so this
    # stays a JSON blob rather than structured columns. None when the registry
    # does not implement the Referrers API (unknown, not "clean").
    scan_result_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint(
            "registry", "repo", "digest", name="uq_container_image_registry_repo_digest"
        ),
    )


__all__ = ["ContainerImage"]
