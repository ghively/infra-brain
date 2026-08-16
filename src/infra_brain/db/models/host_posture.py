"""PCI-relevant host posture tables (MR-J / INV-4).

Bucket: HostCertificate, HostSecurityPosture, HostShare, WindowsLocalUser,
        WindowsLocalGroupMember, WindowsLogonEvent.

docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md flagged these as
"playbook data with no home anywhere" — PCI Req 1/4/5-relevant (certificates,
firewall/AV/RDP/UAC/SSH/SELinux posture, SMB/NFS shares, Windows local
admins/groups) that the fleet-ansible playbook already collects but infra-brain
never persisted. These tables are the home; agents/windows.py (WinRM-based,
see tools/winrm_client.py) is the first writer wired to them — see the MR-J
report for what is real-and-tested vs. plumbing-only pending live WinRM access.

Design notes:
- Every table keys off ``resource_id`` (nullable-safe FK to ``resources.id``)
  rather than a Linux/Windows-specific host table, since certificates and
  shares are collected on both OS families.
- WindowsLocalUser/WindowsLocalGroupMember are Windows-only in practice (no
  Linux writer exists in this MR — LinuxUser already covers Linux local
  accounts) but are not modeled as such structurally; the resource's
  ``domain`` column disambiguates.
- WindowsLogonEvent is schema-only in this MR (no writer wired) — event-log
  parsing via WinRM was triaged into the long-tail item as point-in-time data
  that arguably belongs in a metrics/event pipeline rather than a relational
  snapshot table; see the MR-J report.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONB, _now, _uuid


class HostCertificate(Base):
    """One row per certificate discovered in a host's certificate store(s)."""

    __tablename__ = "host_certificates"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # e.g. "LocalMachine/My", "LocalMachine/Root", "linux:/etc/ssl/certs"
    store: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Thumbprint is the natural per-cert identity; falls back to an empty
    # string (never NULL) so the unique constraint below stays well-defined
    # even for a source that cannot supply one.
    thumbprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    not_before: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    days_until_expiry: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("resource_id", "store", "thumbprint", name="uq_host_cert_natkey"),
    )


class HostSecurityPosture(Base):
    """One row per host summarizing firewall/AV/RDP/UAC/SSH/SELinux posture.

    Fields not applicable to a host's OS simply stay NULL (e.g. selinux_mode
    on a Windows host, rdp_enabled on Linux).
    """

    __tablename__ = "host_security_posture"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    firewall_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    firewall_service: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    av_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    av_product: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    av_signature_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rdp_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    uac_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    ssh_password_auth: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    ssh_permit_root_login: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    ssh_pubkey_auth: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # "enforcing" | "permissive" | "disabled"
    selinux_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    apparmor_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class HostShare(Base):
    """A network share (SMB or NFS) exposed by a host."""

    __tablename__ = "host_shares"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "smb" | "nfs"
    share_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # list of {principal, access} dicts — shape varies by share_type/OS.
    permissions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("resource_id", "share_type", "name", name="uq_host_share_natkey"),
    )


class HostFirewallRule(Base):
    """One row per firewall rule (TRK-031).

    Spec decision: firewall RULES get a DEDICATED table — they are NOT folded
    into HostSecurityPosture, which stays a per-host firewall/AV/RDP/UAC/SSH
    *summary* (one row per host). A host has many rules, so a summary column
    cannot hold them; this table is the home for the rule list. First writer is
    the Linux agent (iptables/nftables/firewalld); nothing structurally ties it
    to Linux, so a Windows writer could populate it later too.
    """

    __tablename__ = "host_firewall_rules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # ``table`` is a SQL reserved word — store the netfilter table under
    # ``table_name`` (e.g. "filter", "nat", "mangle"). NULL for backends with no
    # table concept (firewalld zones).
    table_name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Chain/zone the rule belongs to (e.g. "INPUT", "FORWARD", "public").
    chain: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # The rule as emitted by the source tool (``iptables -S`` line, nft rule).
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Target/verdict — ACCEPT/DROP/REJECT/... NULL when a line carries none.
    action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Which backend produced the rule: "iptables" | "nftables" | "firewalld".
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "source",
            "table_name",
            "chain",
            "rule_text",
            name="uq_host_firewall_rule_natkey",
        ),
    )


class WindowsLocalUser(Base):
    """A local (non-domain) Windows user account."""

    __tablename__ = "windows_local_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_logon: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    password_required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    password_never_expires: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("resource_id", "username", name="uq_windows_local_user_natkey"),
    )


class WindowsLocalGroupMember(Base):
    """Membership of a local Windows group (e.g. Administrators)."""

    __tablename__ = "windows_local_group_members"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_name: Mapped[str] = mapped_column(String(128), nullable=False)
    member_name: Mapped[str] = mapped_column(String(256), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint(
            "resource_id", "group_name", "member_name", name="uq_windows_local_group_member_natkey"
        ),
    )


class WindowsLogonEvent(Base):
    """A Windows logon attempt (success/failure). Schema-only in this MR — see
    the module docstring; no collector is wired to this table yet."""

    __tablename__ = "windows_logon_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "success" | "failed"
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    logon_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


__all__ = [
    "HostCertificate",
    "HostFirewallRule",
    "HostSecurityPosture",
    "HostShare",
    "WindowsLocalUser",
    "WindowsLocalGroupMember",
    "WindowsLogonEvent",
]
