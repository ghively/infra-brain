"""Curated host purpose / VLAN map (MR-J item 3).

The fleet-ansible ``system_inventory.yml`` play carries a hand-maintained
mapping of ~55 hosts to their operational purpose and VLAN — described in
docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md as "curated
knowledge that exists nowhere else" in infra-brain's data model.
``inventory_reconcile.py`` already reads the Ansible inventory file from the
same GitLab project family but never this purpose/VLAN map.

This table is the home for it once ingested; see
``tools/host_purpose_map.py`` for the loader/parser and
``agents/inventory_reconcile.py``'s ``_sync_host_purpose_map`` for the sync
entrypoint. See the MR-J report for the exact source-file assumption made
(the map was not directly inspectable from this worktree).
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class HostPurposeMap(Base):
    """One row per curated host purpose/VLAN entry."""

    __tablename__ = "host_purpose_map"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    hostname: Mapped[str] = mapped_column(String(256), nullable=False)
    purpose: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vlan: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subnet: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # "<gitlab_project_id>:<file_path>" the entry was last loaded from.
    source: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    __table_args__ = (UniqueConstraint("hostname", name="uq_host_purpose_map_hostname"),)


__all__ = ["HostPurposeMap"]
