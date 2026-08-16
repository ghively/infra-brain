"""TRK-031: model-shape tests for the new Linux posture stores.

Two new tables land in this item:

* ``host_firewall_rules`` (host_posture.py) — one row per firewall rule,
  DEDICATED table (spec decision: firewall RULES are NOT folded into
  host_security_posture, which stays a per-host firewall/AV/RDP summary).
* ``linux_pending_updates`` (os_inventory.py) — one row per pending package
  update, keyed to ``linux_hosts`` exactly like LinuxPackage/LinuxPort.

These assert the column surface + nullability + FK/natkey conventions so a
later schema drift is caught here, not in production.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect

from infra_brain.db.models import (
    HostFirewallRule,
    LinuxHost,
    LinuxPendingUpdate,
    Resource,
)


def _cols(model):
    return {c.name: c for c in sa_inspect(model).columns}


# --- host_firewall_rules -----------------------------------------------------


def test_host_firewall_rule_column_surface():
    cols = _cols(HostFirewallRule)
    assert HostFirewallRule.__tablename__ == "host_firewall_rules"
    # host ref FK -> resources.id (mirrors HostShare/HostCertificate)
    assert "resource_id" in cols
    assert not cols["resource_id"].nullable
    fks = {fk.column.table.name for fk in cols["resource_id"].foreign_keys}
    assert "resources" in fks
    # chain/table
    assert "chain" in cols
    assert "table_name" in cols  # "table" is a reserved word -> table_name
    # rule text is required
    assert "rule_text" in cols
    assert not cols["rule_text"].nullable
    # action (ACCEPT/DROP/REJECT) — nullable (a policy line may lack one)
    assert "action" in cols and cols["action"].nullable
    # source (iptables/nftables/firewalld) — required discriminator
    assert "source" in cols and not cols["source"].nullable
    assert "collected_at" in cols


def test_host_firewall_rule_roundtrip(db):
    r = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="linux_host",
        name="fw01",
        source="LinuxAgent",
        last_seen=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    rule = HostFirewallRule(
        resource_id=r.id,
        table_name="filter",
        chain="INPUT",
        rule_text="-A INPUT -p tcp --dport 22 -j ACCEPT",
        action="ACCEPT",
        source="iptables",
    )
    db.add(rule)
    db.flush()
    got = db.get(HostFirewallRule, rule.id)
    assert got.chain == "INPUT"
    assert got.action == "ACCEPT"
    assert got.source == "iptables"


# --- linux_pending_updates ---------------------------------------------------


def test_linux_pending_update_column_surface():
    cols = _cols(LinuxPendingUpdate)
    assert LinuxPendingUpdate.__tablename__ == "linux_pending_updates"
    # host ref FK -> linux_hosts.id (mirrors LinuxPackage/LinuxPort/LinuxCron)
    assert "host_id" in cols
    fks = {fk.column.table.name for fk in cols["host_id"].foreign_keys}
    assert "linux_hosts" in fks
    # package name is required
    assert "package" in cols and not cols["package"].nullable
    # current -> available version (both optional; either can be unknown)
    assert "current_version" in cols and cols["current_version"].nullable
    assert "available_version" in cols and cols["available_version"].nullable
    # security bool
    assert "security" in cols
    assert "collected_at" in cols


def test_linux_pending_update_roundtrip(db):
    r = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="linux_host",
        name="upd01",
        source="LinuxAgent",
        last_seen=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    host = LinuxHost(resource_id=r.id, distro="Ubuntu", kernel="5.15.0", arch="x86_64")
    db.add(host)
    db.flush()
    upd = LinuxPendingUpdate(
        host_id=host.id,
        package="openssl",
        current_version="3.0.2-0ubuntu1.10",
        available_version="3.0.2-0ubuntu1.15",
        security=True,
        manager="apt",
    )
    db.add(upd)
    db.flush()
    got = db.get(LinuxPendingUpdate, upd.id)
    assert got.package == "openssl"
    assert got.available_version == "3.0.2-0ubuntu1.15"
    assert got.security is True
