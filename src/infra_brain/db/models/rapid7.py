"""Rapid7 InsightVM relational model.

Bucket: VulnQueueItem, R7Asset, R7Site, R7Tag, R7Software, R7AssetConfig,
        R7AssetUser, R7AssetAddress, R7Vulnerability, R7Solution,
        R7VulnSolution, R7VulnCve.

Design notes (preserved from original models.py):
- ``r7_asset_id`` / ``r7_site_id`` / ``r7_tag_id`` are the upstream Rapid7
  integer ids and the natural upsert key.
- Vulnerability/solution detail is keyed by the Rapid7 SLUG, not the asset.
- ``r7_vulnerabilities`` and ``r7_solutions`` stand alone (no FK to r7_assets).
- The link is by string slug (NOT a DB FK) through ``r7_vuln_solutions`` and
  ``r7_vuln_cves`` — query these directly rather than via an ORM relationship.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONB, _now, _uuid


class VulnQueueItem(Base):
    __tablename__ = "vuln_queue"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"))
    cve_id: Mapped[str] = mapped_column(String(32))
    kb_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    sla_due: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Natural key for the Rapid7 vuln_queue feeder upsert (MR3).
    __table_args__ = (Index("ix_vuln_queue_resource_cve", "resource_id", "cve_id", unique=True),)


class R7Asset(Base):
    """One Rapid7 InsightVM asset, keyed on the upstream ``r7_asset_id``.

    Typed columns hold the OS (string + broken-out osFingerprint fields), risk
    scores, and the six Rapid7 vulnerability-band counts (Rapid7 uses
    critical/severe/moderate — there is NO "high"). The large nested lists
    (software ~140/asset, users, configurations, addresses, ids, hostNames) land
    in the JSONB ``details`` overflow rather than typed columns.
    """

    __tablename__ = "r7_assets"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    r7_asset_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    mac: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # ``os`` is the Rapid7 list-item OS STRING (the field the old code wrongly
    # read as a dict). The os_* columns are broken out from ``osFingerprint``.
    os: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    os_family: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    os_product: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    os_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    os_vendor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    os_architecture: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    asset_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Rapid7 vulnerability bands (critical/severe/moderate — NO "high").
    vuln_critical: Mapped[int] = mapped_column(Integer, default=0)
    vuln_severe: Mapped[int] = mapped_column(Integer, default=0)
    vuln_moderate: Mapped[int] = mapped_column(Integer, default=0)
    vuln_total: Mapped[int] = mapped_column(Integer, default=0)
    vuln_exploits: Mapped[int] = mapped_column(Integer, default=0)
    vuln_malware_kits: Mapped[int] = mapped_column(Integer, default=0)
    assessed_for_vulnerabilities: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # MR7: deep enrichment children, promoted out of ``details`` into typed tables.
    software: Mapped[list["R7Software"]] = relationship(back_populates="asset")
    configs: Mapped[list["R7AssetConfig"]] = relationship(back_populates="asset")
    asset_users: Mapped[list["R7AssetUser"]] = relationship(back_populates="asset")
    addresses: Mapped[list["R7AssetAddress"]] = relationship(back_populates="asset")


class R7Site(Base):
    """A Rapid7 InsightVM site, keyed on the upstream ``r7_site_id``."""

    __tablename__ = "r7_sites"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    r7_site_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    asset_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    importance: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    site_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_scan_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class R7Tag(Base):
    """A Rapid7 InsightVM tag, keyed on the upstream ``r7_tag_id``."""

    __tablename__ = "r7_tags"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    r7_tag_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    tag_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class R7Software(Base):
    """A software product installed on a Rapid7 asset (~74-140 per asset)."""

    __tablename__ = "r7_software"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("r7_assets.id"), index=True)
    r7_asset_id: Mapped[int] = mapped_column(Integer, index=True)
    product: Mapped[str] = mapped_column(String(256))
    vendor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    software_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    asset: Mapped["R7Asset"] = relationship(back_populates="software")
    __table_args__ = (
        UniqueConstraint("asset_id", "product", "version", name="uq_r7_software_natkey"),
        # The fleet software endpoint builds a DISTINCT vendor dropdown
        # (SELECT DISTINCT vendor ... WHERE vendor IS NOT NULL ORDER BY vendor)
        # and filters by vendor over ~335k rows; this turns that into an
        # index scan instead of a full-table scan + sort (S-10/11/12, MR-F).
        Index("ix_r7_software_vendor", "vendor"),
    )


class R7AssetConfig(Base):
    """A system/hardware spec key-value for a Rapid7 asset (cpuinfo, memory,
    OS-release, kernel, timezone, ...)."""

    __tablename__ = "r7_asset_config"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("r7_assets.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    asset: Mapped["R7Asset"] = relationship(back_populates="configs")
    __table_args__ = (UniqueConstraint("asset_id", "name", name="uq_r7_asset_config_natkey"),)


class R7AssetUser(Base):
    """A local user account observed on a Rapid7 asset."""

    __tablename__ = "r7_asset_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("r7_assets.id"), index=True)
    username: Mapped[str] = mapped_column(String(256))
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    asset: Mapped["R7Asset"] = relationship(back_populates="asset_users")
    __table_args__ = (UniqueConstraint("asset_id", "username", name="uq_r7_asset_user_natkey"),)


class R7AssetAddress(Base):
    """An IP/MAC address pair for a Rapid7 asset."""

    __tablename__ = "r7_asset_addresses"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("r7_assets.id"), index=True)
    ip: Mapped[str] = mapped_column(String(64))
    mac: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    asset: Mapped["R7Asset"] = relationship(back_populates="addresses")
    __table_args__ = (UniqueConstraint("asset_id", "ip", name="uq_r7_asset_address_natkey"),)


class R7Vulnerability(Base):
    """A Rapid7 vulnerability definition, keyed on the upstream SLUG
    (``r7_vuln_id``, e.g. "microsoft-asp_net_core-cve-2023-36038"). Shared
    across assets — no FK to r7_assets."""

    __tablename__ = "r7_vulnerabilities"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", name="fk_r7_vuln_resource", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    r7_vuln_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cvss_v3_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cvss_v3_vector: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    cvss_v2_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exploits: Mapped[int] = mapped_column(Integer, default=0)
    malware_kits: Mapped[int] = mapped_column(Integer, default=0)
    denial_of_service: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    fix_available: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    pci_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pci_fail: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    published: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    categories: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    cves: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class R7Solution(Base):
    """A Rapid7 remediation solution, keyed on the upstream solution id
    (``r7_solution_id``). Carries the human fix steps."""

    __tablename__ = "r7_solutions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", name="fk_r7_solution_resource", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    r7_solution_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    steps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    solution_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    estimate: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fix_available: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class R7VulnSolution(Base):
    """Many-to-many link between a vulnerability slug and a solution id.
    Both sides key by upstream string id (not a DB FK) — queried directly."""

    __tablename__ = "r7_vuln_solutions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    r7_vuln_id: Mapped[str] = mapped_column(String(128), index=True)
    r7_solution_id: Mapped[str] = mapped_column(String(128), index=True)
    __table_args__ = (
        UniqueConstraint("r7_vuln_id", "r7_solution_id", name="uq_r7_vuln_solution_natkey"),
    )


class R7VulnCve(Base):
    """Bridge mapping a Rapid7 vulnerability SLUG to the CVE ids it covers.

    Rapid7 keys vulnerabilities by an internal slug (``r7_vuln_id``), but
    ``vuln_queue`` keys by canonical ``CVE-YYYY-N`` ids. The two therefore do
    not join directly. This bridge explodes ``r7_vulnerabilities.cves`` (a JSONB
    array) into one row per (slug, CVE) so callers can walk:
    ``vuln_queue.cve_id → r7_vuln_cves.cve_id → r7_vuln_id → r7_vulnerabilities``
    (CVSS/exploits/PCI) and on to ``r7_vuln_solutions → r7_solutions`` (fix
    steps). Link is by string slug, NOT a DB FK — queried directly."""

    __tablename__ = "r7_vuln_cves"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    r7_vuln_id: Mapped[str] = mapped_column(String(128), index=True)
    cve_id: Mapped[str] = mapped_column(String(32), index=True)
    __table_args__ = (UniqueConstraint("r7_vuln_id", "cve_id", name="uq_r7_vuln_cve_natkey"),)


class R7AssetSite(Base):
    """Bridge mapping a Rapid7 asset to the scan sites it belongs to (TRK-104).

    Rapid7 assets are many-to-many with sites (an asset can be scanned by
    several sites). The asset list-item carries a ``sites`` array of upstream
    integer site ids. Both sides key by upstream integer id (``r7_asset_id`` /
    ``r7_site_id``), NOT a DB FK — queried directly, matching the R7VulnCve /
    R7VulnSolution bridge pattern. Populated per-asset (delete-then-insert) in
    vuln._write_asset_children."""

    __tablename__ = "r7_asset_sites"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    r7_asset_id: Mapped[int] = mapped_column(Integer, index=True)
    r7_site_id: Mapped[int] = mapped_column(Integer, index=True)
    __table_args__ = (UniqueConstraint("r7_asset_id", "r7_site_id", name="uq_r7_asset_site"),)


__all__ = [
    "VulnQueueItem",
    "R7Asset",
    "R7Site",
    "R7Tag",
    "R7Software",
    "R7AssetConfig",
    "R7AssetUser",
    "R7AssetAddress",
    "R7Vulnerability",
    "R7Solution",
    "R7VulnSolution",
    "R7VulnCve",
    "R7AssetSite",
]
