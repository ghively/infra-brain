"""Commercial software license entitlement (GitLab issue #97).

Distinct from ``EolRegistry`` (eol.py, tracks end-of-life dates) and
``ComplianceViolation`` (compliance.py, policy-as-code rule findings):
``SoftwareLicense`` is the operator-seeded entitlement side of a license
compliance reconciliation — "how many seats/cores did we buy for product X,
and when does that grant expire." ``LicensingAgent`` (agents/licensing.py)
reconciles this against the installed base already visible via
``HAS_SOFTWARE`` graph edges / ``WindowsSoftware``/``R7Software``/
``LinuxPackage`` rows (os_inventory.py, rapid7.py) and surfaces
over-/under-entitlement findings as ``ComplianceViolation`` rows.

Generalizes the existing per-vCenter ``VsphereLicense`` pattern
(db/models/vsphere.py) — total/used seats keyed to one external system — to
any commercial software product visible in the already-collected installed
base: Windows Server CALs, database licensing, or any other per-seat/
per-core commercial software.

No FK to ``resources`` — a license entitlement is not itself a piece of
observed infrastructure (nothing was collected from a live system to produce
this row); it is manually seeded ground truth, same epistemic category as
the ``migration_path`` an operator sets by hand on ``EolRegistry``.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class SoftwareLicense(Base):
    """One entitlement grant: how many seats/cores of *product* were bought.

    ``product`` is matched case-insensitively (exact match, then substring)
    against the ``product`` key in a ``domain="software"`` Resource's
    metadata — see ``agents/graph_maintenance.py``'s HAS_SOFTWARE block for
    where that metadata is set (``{"product": ..., "version": ...}``).

    ``license_type`` is a free-text label (e.g. "per-seat", "per-core",
    "subscription", "cal") — not an enum, since commercial licensing
    vocabularies vary too widely by vendor to usefully constrain.

    Exactly one of ``seats_entitled`` / ``cores_entitled`` is expected to be
    set per row (per-seat vs. per-core licensing are mutually exclusive
    metering models for a given grant) but neither is DB-enforced NOT NULL —
    an operator seeding a row for a subscription with no fixed seat/core cap
    (e.g. site license) may legitimately leave both null, and the
    reconciliation pass in ``LicensingAgent`` skips entitlement comparison
    for such rows rather than treating null as zero.
    """

    __tablename__ = "software_licenses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    product: Mapped[str] = mapped_column(String(256), nullable=False)
    license_type: Mapped[str] = mapped_column(String(64), nullable=False, default="per-seat")
    vendor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    seats_entitled: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cores_entitled: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-text provenance of the entitlement grant itself (PO number, order
    # confirmation link, "manually seeded 2026-07-30", etc.) — distinct from
    # a collector's ``source`` column, since no collector produces this row.
    source: Mapped[str] = mapped_column(String(256), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("product", "license_type", name="uq_software_license_product_type"),
    )


__all__ = ["SoftwareLicense"]
