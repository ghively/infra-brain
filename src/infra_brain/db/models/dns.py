"""DNS zone/record models — GitLab issue #95.

DNS was previously only a side-effect check during host reconciliation, never
an audited resource in its own right. ``DnsZone`` captures one authoritative
zone's SOA/serial metadata (one row per zone, 1:1 with a ``domain="dns"``
``Resource``, mirroring the ``LinuxHost``/``VsphereVm`` "one detail row per
collected Resource" convention). ``DnsRecord`` captures the individual
records within a zone (A/AAAA/CNAME/MX/TXT/SRV/NS), natural-keyed on
``(zone_id, name, type, value)`` so the same record surviving a re-collect
upserts in place instead of duplicating, while a record whose ``value``
actually changed (e.g. an A record repointed) creates a distinguishable new
row relative to the old value — which is what makes stale-record / orphaned-
CNAME drift detection possible downstream (DriftDetector diffs
``DnsRecord`` snapshots the same way it diffs any other domain).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, _now, _uuid


class DnsZone(Base):
    """One authoritative DNS zone (e.g. ``example.com.``) and its SOA state."""

    __tablename__ = "dns_zones"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # 1:1 with the generic Resource row the base collect-phase upserts
    # (domain="dns", type="dns_zone", name=<zone name>) — same pattern as
    # LinuxHost.resource_id / VsphereVm.resource_id.
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    # SOA fields (RFC 1035 §3.3.13).
    serial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    primary_ns: Mapped[str | None] = mapped_column(String(256), nullable=True)
    admin_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    refresh_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expire_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when the last collection cycle obtained a full zone transfer
    # (AXFR) — most authoritative servers refuse AXFR from an arbitrary
    # client, so this is commonly False even on a healthy, reachable zone;
    # in that case only SOA-derived metadata above is populated and
    # ``records`` reflects whatever previous successful transfer (if any)
    # is still on file, not "the zone has no records."
    axfr_succeeded: Mapped[bool] = mapped_column(default=False)
    last_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    records: Mapped[list["DnsRecord"]] = relationship(
        back_populates="zone", cascade="all, delete-orphan"
    )


class DnsRecord(Base):
    """One resource record within a zone. Natural key: zone + name + type + value."""

    __tablename__ = "dns_records"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dns_zones.id"))
    name: Mapped[str] = mapped_column(String(256), index=True)
    # A | AAAA | CNAME | MX | TXT | SRV | NS
    type: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[str] = mapped_column(Text)
    ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # MX preference / SRV priority — unused for other record types.
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    zone: Mapped["DnsZone"] = relationship(back_populates="records")

    __table_args__ = (
        UniqueConstraint("zone_id", "name", "type", "value", name="uq_dns_record_natural_key"),
    )


__all__ = ["DnsRecord", "DnsZone"]
