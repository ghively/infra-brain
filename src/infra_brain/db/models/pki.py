"""PKI / Certificate Authority tracking (GitLab issue #94).

Distinct from per-host TLS expiry (``host_posture.HostCertificate`` — leaf
certs discovered on a host's own trust store, already wired to the
``HAS_CERTIFICATE`` convergence edge by ``graph_maintenance.py``): this
module tracks the CA infrastructure itself — root/intermediate CA chain
validity, CRL/OCSP responder health, and which already-tracked leaf certs
chain to which tracked intermediate CA.

Bucket: CertificateAuthority.

Design notes (from the issue):
- ``ca_type`` is ``"root"`` or ``"intermediate"``.
- Keyed off ``resource_id`` (nullable-safe FK to ``resources.id``), matching
  the ``host_posture`` convention, so a CA is a first-class Resource
  (domain="pki", type="certificate_authority") like every other collected
  entity.
- ``crl_url`` / ``ocsp_url`` are optional — populated when known (derived
  chain data rarely carries them; a future manual-seed path could add them).
  When present, ``agents/pki.py`` performs a read-only GET health check
  against each and records the result here.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class CertificateAuthority(Base):
    """One row per tracked root or intermediate Certificate Authority."""

    __tablename__ = "certificate_authorities"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    # "root" | "intermediate"
    ca_type: Mapped[str] = mapped_column(String(16), nullable=False)
    issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crl_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocsp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Last read-only health check result against crl_url/ocsp_url, when set.
    # "reachable" | "unreachable" | None (never checked, e.g. no url set).
    crl_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    crl_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ocsp_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ocsp_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("name", "ca_type", name="uq_certificate_authority_natkey"),)
