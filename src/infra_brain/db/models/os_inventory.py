"""OS-level host inventory models.

Bucket: LinuxHost, LinuxPackage, LinuxService, LinuxUser, LinuxCron, LinuxPort,
        LinuxMount, LinuxNic, WindowsPatchState, WindowsService,
        WindowsSoftware, EolRegistry.

LinuxMount/LinuxNic (MR-J long-tail pick): ``ansible -m setup`` ALREADY
returns ``ansible_mounts`` (per-filesystem usage) and interface facts
(``ansible_interfaces`` + per-interface ``ansible_<name>`` dicts) — these two
tables persist that already-collected-but-dropped data with ZERO new gather
cost, unlike every other INV long-tail category which would need a new probe.
See docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md's "Gathered by
setup but not persisted" / "setup gives mounts but nothing persisted
per-volume" findings.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONB, _now, _uuid


class LinuxHost(Base):
    __tablename__ = "linux_hosts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    distro: Mapped[str] = mapped_column(String(128))
    kernel: Mapped[str] = mapped_column(String(128))
    arch: Mapped[str] = mapped_column(String(32))
    last_boot: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    uptime: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    packages: Mapped[list["LinuxPackage"]] = relationship(back_populates="host")
    services: Mapped[list["LinuxService"]] = relationship(back_populates="host")
    users: Mapped[list["LinuxUser"]] = relationship(back_populates="host")
    crons: Mapped[list["LinuxCron"]] = relationship(back_populates="host")
    ports: Mapped[list["LinuxPort"]] = relationship(back_populates="host")
    mounts: Mapped[list["LinuxMount"]] = relationship(back_populates="host")
    nics: Mapped[list["LinuxNic"]] = relationship(back_populates="host")
    pending_updates: Mapped[list["LinuxPendingUpdate"]] = relationship(back_populates="host")


class LinuxPackage(Base):
    __tablename__ = "linux_packages"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(128))
    manager: Mapped[str] = mapped_column(String(16))
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    host: Mapped["LinuxHost"] = relationship(back_populates="packages")


class LinuxService(Base):
    __tablename__ = "linux_services"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    host: Mapped["LinuxHost"] = relationship(back_populates="services")


class LinuxUser(Base):
    __tablename__ = "linux_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"), index=True)
    username: Mapped[str] = mapped_column(String(128))
    shell: Mapped[str] = mapped_column(String(128))
    sudo: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    host: Mapped["LinuxHost"] = relationship(back_populates="users")


class LinuxCron(Base):
    __tablename__ = "linux_crons"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"), index=True)
    owner: Mapped[str] = mapped_column(String(128))
    schedule: Mapped[str] = mapped_column(String(128))
    command: Mapped[str] = mapped_column(Text)
    host: Mapped["LinuxHost"] = relationship(back_populates="crons")


class LinuxPort(Base):
    __tablename__ = "linux_ports"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    proto: Mapped[str] = mapped_column(String(8))
    process: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    state: Mapped[str] = mapped_column(String(32))
    host: Mapped["LinuxHost"] = relationship(back_populates="ports")


class LinuxMount(Base):
    """Per-filesystem usage, from the already-gathered ``ansible_mounts`` fact."""

    __tablename__ = "linux_mounts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"))
    mount: Mapped[str] = mapped_column(String(256))
    device: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    fstype: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_total_gb: Mapped[Optional[float]] = mapped_column(nullable=True)
    size_available_gb: Mapped[Optional[float]] = mapped_column(nullable=True)
    host: Mapped["LinuxHost"] = relationship(back_populates="mounts")
    __table_args__ = (UniqueConstraint("host_id", "mount", name="uq_linux_mount_natkey"),)


class LinuxNic(Base):
    """Per-interface network detail, from already-gathered setup facts
    (``ansible_interfaces`` + per-interface ``ansible_<name>``/
    ``ansible_default_ipv4`` dicts)."""

    __tablename__ = "linux_nics"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"))
    name: Mapped[str] = mapped_column(String(64))
    mac: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ipv4: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ipv6: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    speed_mbps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    host: Mapped["LinuxHost"] = relationship(back_populates="nics")
    __table_args__ = (UniqueConstraint("host_id", "name", name="uq_linux_nic_natkey"),)


class LinuxPendingUpdate(Base):
    """One row per pending package update on a Linux host (TRK-031).

    Populated from ``apt list --upgradable`` (Debian/Ubuntu) or
    ``dnf check-updates`` (RHEL/Fedora). Keyed to ``linux_hosts`` via
    ``host_id`` exactly like LinuxPackage/LinuxPort/LinuxCron — this is the
    per-package Linux detail store, distinct from WindowsPatchState which is a
    per-host patch *summary* (pending_count / kb_list). ``security`` flags
    updates the package manager marks as coming from a security channel.
    """

    __tablename__ = "linux_pending_updates"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linux_hosts.id"))
    package: Mapped[str] = mapped_column(String(256), nullable=False)
    current_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    available_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    security: Mapped[bool] = mapped_column(Boolean, default=False)
    manager: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    host: Mapped["LinuxHost"] = relationship(back_populates="pending_updates")
    __table_args__ = (
        UniqueConstraint(
            "host_id", "package", "available_version", name="uq_linux_pending_update_natkey"
        ),
    )


class WindowsPatchState(Base):
    __tablename__ = "windows_patch_state"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    hostname: Mapped[str] = mapped_column(String(256))
    kb_list: Mapped[list[str]] = mapped_column(JSONB, default=list)
    last_patched: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    winrm_status: Mapped[str] = mapped_column(String(32), default="unknown")


class WindowsService(Base):
    """A Windows service discovered on a resource host (one row per service per host)."""

    __tablename__ = "windows_services"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # e.g. "Running", "Stopped"
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # e.g. "Automatic", "Manual", "Disabled"
    start_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("resource_id", "name", name="uq_windows_services_resource_name"),
    )


class WindowsSoftware(Base):
    """An installed software product on a Windows resource host."""

    __tablename__ = "windows_software"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    publisher: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    install_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("resource_id", "name", name="uq_windows_software_resource_name"),
    )


class EolRegistry(Base):
    __tablename__ = "eol_registry"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), index=True)
    asset_name: Mapped[str] = mapped_column(String(256))
    eol_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pci_risk_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    migration_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Merge key for derived + manual EOL entries (MR3); non-unique by design.
    __table_args__ = (Index("ix_eol_registry_asset_name", "asset_name"),)


__all__ = [
    "LinuxHost",
    "LinuxPackage",
    "LinuxService",
    "LinuxUser",
    "LinuxCron",
    "LinuxPort",
    "LinuxMount",
    "LinuxNic",
    "LinuxPendingUpdate",
    "WindowsPatchState",
    "WindowsService",
    "WindowsSoftware",
    "EolRegistry",
]
