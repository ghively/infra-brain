"""Tests for EOLAgent — auto-derive eol_registry from OS inventory (MR3).

The agent now INVERTS the old "read registry then enrich" flow: it derives
distinct OSes from collected inventory, normalizes each to an endoflife.date
(product, cycle), looks up the EOL date, and upserts eol_registry by asset_name.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from infra_brain.agents.eol import EOLAgent, _suggest_migration
from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.db.models import (
    CollectionRun,
    EolRegistry,
    HostIdentity,
    R7Asset,
    Resource,
    VsphereVm,
)
from infra_brain.etl.base import CollectOutcome
from infra_brain.tools.eol import normalize_fingerprint, normalize_os_string

MODULE = "infra_brain.agents.eol"


def _agent():
    agent = EOLAgent.__new__(EOLAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


# --- OS-string normalizer ----------------------------------------------------


def test_normalizer_maps_common_oses():
    assert normalize_os_string("CentOS 7 (64-bit)") == ("centos", "7", "CentOS 7")
    assert normalize_os_string("Microsoft Windows Server 2019") == (
        "windows-server",
        "2019",
        "Windows Server 2019",
    )
    assert normalize_os_string("Windows Server 2012 R2") == (
        "windows-server",
        "2012-R2",
        "Windows Server 2012 R2",
    )
    assert normalize_os_string("Red Hat Enterprise Linux 8") == ("rhel", "8", "RHEL 8")
    assert normalize_os_string("Rocky Linux 9") == ("rocky-linux", "9", "Rocky Linux 9")
    assert normalize_os_string("SLES 15") == ("sles", "15", "SLES 15")
    assert normalize_os_string("Ubuntu 22.04.3 LTS") == ("ubuntu", "22.04", "Ubuntu 22.04")


def test_normalizer_maps_debian():
    """TRK-134: Debian was conspicuously absent from _OS_RULES even though
    MIGRATION_MAP already had "debian 9"/"debian 10" entries — the clearest
    proof the table was under-scoped."""
    assert normalize_os_string("Debian GNU/Linux 10 (buster)") == ("debian", "10", "Debian 10")
    assert normalize_os_string("Debian GNU/Linux 9 (stretch)") == ("debian", "9", "Debian 9")
    assert normalize_os_string("Debian GNU/Linux 11 (bullseye)") == ("debian", "11", "Debian 11")
    assert normalize_os_string("Debian GNU/Linux 12 (bookworm)") == ("debian", "12", "Debian 12")


def test_normalizer_maps_debian_13():
    """GitLab #146 re-validation (2026-08-02): the live fleet's two Debian
    13.6 hosts were silently skipped because _OS_RULES stopped at Debian 12 —
    the confirmed remnant of the "fleet coverage gap" claim. The exact live
    string is "Debian 13.6" (distro + version joined by EOLAgent), and the
    minor-version digit must not leak into the cycle."""
    assert normalize_os_string("Debian 13.6") == ("debian", "13", "Debian 13")
    assert normalize_os_string("Debian GNU/Linux 13 (trixie)") == ("debian", "13", "Debian 13")
    # A minor version ".13" must never be mistaken for major version 13.
    assert normalize_os_string("Debian 12.13") == ("debian", "12", "Debian 12")


def test_normalizer_maps_other_previously_missing_oses():
    """TRK-134: Windows 10/11 desktop, AlmaLinux, Oracle Linux, ESXi, Amazon
    Linux were also missing from _OS_RULES."""
    assert normalize_os_string("Microsoft Windows 10 Pro") == ("windows", "10", "Windows 10")
    assert normalize_os_string("Microsoft Windows 11 Enterprise") == (
        "windows",
        "11",
        "Windows 11",
    )
    assert normalize_os_string("AlmaLinux 9.3") == ("almalinux", "9", "AlmaLinux 9")
    assert normalize_os_string("Oracle Linux Server 8.9") == ("oracle-linux", "8", "Oracle Linux 8")
    assert normalize_os_string("VMware ESXi 8.0.2") == ("vmware-esxi", "8.0", "VMware ESXi 8.0")
    assert normalize_os_string("Amazon Linux 2023") == (
        "amazon-linux",
        "2023",
        "Amazon Linux 2023",
    )
    assert normalize_os_string("Amazon Linux 2") == ("amazon-linux", "2", "Amazon Linux 2")


def test_normalizer_rhel_centos_rocky_minor_version_does_not_leak_into_major():
    """RHEL/CentOS/Rocky major-version rules were missing the ``(?<!\\.)``
    dot-guard that sibling rules (Windows, AlmaLinux, Oracle Linux, Debian,
    Amazon Linux) already carry, so a trailing minor-version digit was
    misread as the major version (e.g. the "9" in "8.9" matched the RHEL 9
    rule, which is checked before the RHEL 8 rule, before "8" ever got a
    chance). These are exactly the RPM-based distros that commonly report an
    X.Y version string."""
    assert normalize_os_string("Red Hat Enterprise Linux Server release 8.9 (Ootpa)") == (
        "rhel",
        "8",
        "RHEL 8",
    )
    assert normalize_os_string("CentOS Linux release 6.7 (Final)") == ("centos", "6", "CentOS 6")
    assert normalize_os_string("Rocky Linux 8.9") == ("rocky-linux", "8", "Rocky Linux 8")


def test_normalizer_returns_none_for_unmapped():
    assert normalize_os_string("Some Weird Appliance OS") is None
    assert normalize_os_string("") is None
    assert normalize_os_string(None) is None


# --- osFingerprint normalizer (MR6) -----------------------------------------


def test_fingerprint_normalizer_maps_oses():
    assert normalize_fingerprint("Windows", "Windows Server 2019 Datacenter Edition", "1809") == (
        "windows-server",
        "2019",
        "Windows Server 2019",
    )
    assert normalize_fingerprint("Linux", "CentOS Linux", "7") == ("centos", "7", "CentOS 7")
    assert normalize_fingerprint("Linux", "Red Hat Enterprise Linux Server", "8.6") == (
        "rhel",
        "8",
        "RHEL 8",
    )
    assert normalize_fingerprint("Linux", "Rocky Linux", "9.3") == (
        "rocky-linux",
        "9",
        "Rocky Linux 9",
    )
    assert normalize_fingerprint("Linux", "Ubuntu", "22.04.3") == (
        "ubuntu",
        "22.04",
        "Ubuntu 22.04",
    )


def test_fingerprint_normalizer_none_for_unmapped_or_empty():
    assert normalize_fingerprint("Other", "Weird Appliance Firmware", "1") is None
    assert normalize_fingerprint(None, None, None) is None


# --- pci_risk_score ----------------------------------------------------------


def test_pci_risk_score_from_eol_proximity():
    now = datetime(2026, 6, 24, tzinfo=UTC)
    assert EOLAgent._pci_risk_score(now - timedelta(days=1), now) == 90  # past EOL
    assert EOLAgent._pci_risk_score(now + timedelta(days=30), now) == 70  # <90d
    assert EOLAgent._pci_risk_score(now + timedelta(days=200), now) == 40  # <1yr
    assert EOLAgent._pci_risk_score(now + timedelta(days=1000), now) == 10  # far off


def test_pci_risk_score_null_eol_date_is_high_urgency_not_lowest():
    """GitLab #186: a null eol_date used to map to 10 — the LOWEST/least-
    urgent value on the 0-100 (higher = more urgent) scale — silently
    deprioritizing "unknown EOL date" below every known-dated asset except
    the ones furthest from EOL. An unknown date must not score as the least
    urgent case; it now scores 90, on par with a confirmed past-EOL asset."""
    now = datetime(2026, 6, 24, tzinfo=UTC)
    score = EOLAgent._pci_risk_score(None, now)
    assert score != 10, "null eol_date must not score the lowest/least-urgent value"
    assert score == 90
    assert score > EOLAgent._pci_risk_score(now + timedelta(days=1000), now)


# --- end-to-end derive + upsert ---------------------------------------------


def _seed_inventory(engine):
    """Mixed OS inventory across all four sources, including one unmapped."""
    with Session(engine) as s:
        # vSphere VM with clean guest_full_name
        vm_res = Resource(
            id=uuid.uuid4(),
            domain="vsphere",
            type="vsphere_vm",
            name="vm-01",
            source="VsphereAgent",
            zone="corpor",
        )
        s.add(vm_res)
        s.flush()
        s.add(
            VsphereVm(
                id=uuid.uuid4(),
                resource_id=vm_res.id,
                vcenter="vc1",
                moref="vm-1",
                name="vm-01",
                guest_full_name="CentOS 7 (64-bit)",
            )
        )
        # Linux host: distro + version in metadata
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="linux",
                type="linux_host",
                name="lx-01",
                source="LinuxAgent",
                zone="corpor",
                metadata_={"distro": "RedHat", "version": "8.6"},
            )
        )
        # Windows host: os_name + os_version
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="windows",
                type="windows_host",
                name="win-01",
                source="WindowsAgent",
                zone="corpor",
                metadata_={"os_name": "Microsoft Windows Server 2019", "os_version": "10.0.17763"},
            )
        )
        # Rapid7 asset — PRIMARY OS source (MR6): the relational r7_assets row
        # carries the structured osFingerprint fields. CentOS 7 here MERGES with
        # the vSphere CentOS 7 (same friendly label).
        r7_res = Resource(
            id=uuid.uuid4(),
            domain="vuln",
            type="r7_asset",
            name="srv-01",
            source="VulnAgent",
            zone="corpor",
        )
        s.add(r7_res)
        s.flush()
        s.add(
            R7Asset(
                id=uuid.uuid4(),
                resource_id=r7_res.id,
                r7_asset_id=501,
                hostname="srv-01",
                os="CentOS Linux 7.9.2009",
                os_family="Linux",
                os_product="CentOS Linux",
                os_version="7",
            )
        )
        # Unmapped OS — must be skipped, not crash
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="linux",
                type="linux_host",
                name="appliance",
                source="LinuxAgent",
                zone="corpor",
                metadata_={"distro": "WeirdAppliance", "version": "1"},
            )
        )
        s.commit()


_CYCLES = {
    "centos": [{"cycle": "7", "eol": "2024-06-30"}, {"cycle": "8", "eol": "2021-12-31"}],
    "rhel": [{"cycle": "8", "eol": "2029-05-31"}, {"cycle": "9", "eol": "2032-05-31"}],
    "windows-server": [{"cycle": "2019", "eol": "2029-01-09"}],
}


def _mock_cycles(payload, config=None):
    return _CYCLES.get(payload["product"], [])


def test_derive_and_upsert_registry(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_inventory(engine)
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            rows = {r.asset_name: r for r in s.query(EolRegistry).all()}
            # CentOS 7 (from vSphere + rapid7 → MERGED into one), RHEL 8, Win 2019.
            # The "WeirdAppliance" is unmapped → skipped.
            assert set(rows) == {"CentOS 7", "RHEL 8", "Windows Server 2019"}
            centos = rows["CentOS 7"]
            # SQLite drops tzinfo on round-trip — compare on the date.
            assert centos.eol_date.date() == datetime(2024, 6, 30).date()
            assert centos.pci_risk_score == 90  # 2024 EOL is in the past → 90
            assert centos.resource_id is not None
            assert rows["RHEL 8"].pci_risk_score == 10  # 2029 EOL far off
            assert rows["Windows Server 2019"].eol_date.date() == datetime(2029, 1, 9).date()


def test_derive_is_idempotent(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_inventory(engine)
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()
            agent.collect()
            agent._write_eol_registry()  # second pass — same OSes
        with Session(engine) as s:
            assert s.query(EolRegistry).count() == 3  # no duplicates


def test_manual_entry_not_clobbered(session_patcher):
    """A manual add_eol_product entry merges with a derived one by asset_name."""
    with session_patcher(MODULE) as engine:
        # Pre-existing MANUAL entry for CentOS 7 with a migration_path set.
        with Session(engine) as s:
            res = Resource(
                id=uuid.uuid4(),
                domain="eol",
                type="product",
                name="CentOS 7",
                source="mcp",
                zone="corpor",
            )
            s.add(res)
            s.flush()
            s.add(
                EolRegistry(
                    resource_id=res.id,
                    asset_name="CentOS 7",
                    eol_date=datetime(2024, 6, 30, tzinfo=UTC),
                    pci_risk_score=90,
                    migration_path="Migrate to RHEL 9",
                )
            )
            s.commit()
        _seed_inventory(engine)

        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            centos = s.query(EolRegistry).filter_by(asset_name="CentOS 7").all()
            assert len(centos) == 1  # merged, not duplicated
            # migration_path the human set is preserved (derive leaves it untouched).
            assert centos[0].migration_path == "Migrate to RHEL 9"


def _seed_r7_assets(engine, rows):
    """Seed only r7_assets rows (each linked to a fresh r7_asset Resource)."""
    with Session(engine) as s:
        for i, fields in enumerate(rows):
            res = Resource(
                id=uuid.uuid4(),
                domain="vuln",
                type="r7_asset",
                name=fields.get("hostname", f"r7-{i}"),
                source="VulnAgent",
                zone="corpor",
            )
            s.add(res)
            s.flush()
            s.add(R7Asset(id=uuid.uuid4(), resource_id=res.id, r7_asset_id=1000 + i, **fields))
        s.commit()


def test_derive_from_r7_assets_fingerprint(session_patcher):
    """EOL products derive from r7_assets osFingerprint fields (MR6 primary source)."""
    with session_patcher(MODULE) as engine:
        _seed_r7_assets(
            engine,
            [
                {
                    "hostname": "win-201",
                    "os": "Microsoft Windows Server 2019 Datacenter Edition 1809",
                    "os_family": "Windows",
                    "os_product": "Windows Server 2019 Datacenter Edition",
                    "os_version": "1809",
                },
                {
                    "hostname": "lx-77",
                    "os": "CentOS Linux 7.9.2009",
                    "os_family": "Linux",
                    "os_product": "CentOS Linux",
                    "os_version": "7",
                },
                # Unmapped fingerprint → logged + skipped, no row, no crash.
                {
                    "hostname": "appliance-1",
                    "os": "Acme Firmware 3",
                    "os_family": "Other",
                    "os_product": "Acme Firmware",
                    "os_version": "3",
                },
            ],
        )
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            rows = {r.asset_name for r in s.query(EolRegistry).all()}
            # The two mappable fingerprints derived; the appliance was skipped.
            assert rows == {"Windows Server 2019", "CentOS 7"}


def test_derive_from_r7_assets_is_idempotent(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_r7_assets(
            engine,
            [
                {
                    "hostname": "lx-77",
                    "os_family": "Linux",
                    "os_product": "CentOS Linux",
                    "os_version": "7",
                }
            ],
        )
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()
            agent.collect()
            agent._write_eol_registry()
        with Session(engine) as s:
            assert s.query(EolRegistry).count() == 1


# --- TRK-275/GitLab #146+#153: canonical host-identity resolution, reused
# from HostReconcileAgent (not a second resolver) ----------------------------


def test_canonical_host_resource_prefers_higher_priority_source(sqlite_engine):
    """Direct unit test of _canonical_host_resource: given a HostIdentity row
    with multiple legs populated, the highest-priority source per
    HostReconcileAgent._SOURCE_KEYS (r7 before vsphere before linux, ...) is
    returned — the exact anchor-priority host_reconcile itself uses, reused
    rather than reimplemented."""
    with Session(sqlite_engine) as s:
        r7_res = Resource(domain="vuln", type="r7_asset", name="prod-srv-002", source="VulnAgent")
        vsphere_res = Resource(
            domain="vsphere",
            type="vsphere_vm",
            name="prod-lnx-ca-001.example.internal (vcenter-01.corp.example.com)",
            source="VsphereAgent",
        )
        linux_res = Resource(
            domain="linux", type="linux_host", name="prod-lnx-ca-001", source="LinuxAgent"
        )
        s.add_all([r7_res, vsphere_res, linux_res])
        s.flush()
        s.add(
            HostIdentity(
                short_hostname="prod-lnx-ca-001",
                r7_resource_id=r7_res.id,
                vsphere_resource_id=vsphere_res.id,
                linux_resource_id=linux_res.id,
            )
        )
        s.commit()

        # FQDN spelling normalizes to the same short_hostname key.
        rid, priority = EOLAgent._canonical_host_resource(s, "prod-lnx-ca-001.example.internal")
        assert rid == r7_res.id  # r7 outranks vsphere and linux
        assert priority == 0

        # Removing the r7 leg falls through to the next-highest present leg.
        s.query(HostIdentity).filter_by(short_hostname="prod-lnx-ca-001").update(
            {"r7_resource_id": None}
        )
        s.commit()
        rid2, priority2 = EOLAgent._canonical_host_resource(s, "prod-lnx-ca-001")
        assert rid2 == vsphere_res.id
        assert priority2 == 1


def test_canonical_host_resource_falls_back_when_no_identity_row(sqlite_engine):
    """No HostIdentity row yet (host_reconcile hasn't run, or never will for
    this hostname) → falls back to (None, sentinel), same as an unqualified/
    empty hostname."""
    with Session(sqlite_engine) as s:
        assert EOLAgent._canonical_host_resource(s, "never-reconciled-host") == (
            None,
            len(HostReconcileAgent._SOURCE_KEYS),
        )
        assert EOLAgent._canonical_host_resource(s, "") == (
            None,
            len(HostReconcileAgent._SOURCE_KEYS),
        )
        assert EOLAgent._canonical_host_resource(s, None) == (
            None,
            len(HostReconcileAgent._SOURCE_KEYS),
        )


def test_canonical_host_resource_degrades_to_fallback_on_query_error(sqlite_engine, monkeypatch):
    """A host_identities query error must degrade to the raw-Resource fallback,
    not propagate and fail the whole EOL collection run (lc-safety-reviewer
    finding on TRK-275: this lookup previously had no local exception
    handling, so a DB error here took down agent-wide derivation)."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated host_identities query failure")

    with Session(sqlite_engine) as s:
        monkeypatch.setattr(s, "query", _boom)
        assert EOLAgent._canonical_host_resource(s, "prod-lnx-ca-001.example.internal") == (
            None,
            len(HostReconcileAgent._SOURCE_KEYS),
        )


def test_derive_uses_canonical_identity_over_raw_vsphere_name(session_patcher):
    """TRK-275/GitLab #146+#153 end-to-end: a vSphere VM's Resource carries the
    vCenter-qualified, cross-domain name ("prod-lnx-ca-001.example.internal
    (vcenter-01.corp.example.com)") — historically the FIRST resource_id
    seen for its OS, and thus the representative host eol_registry (and the
    dashboard's Host column) showed for that product. Once HostReconcileAgent
    has already reconciled this physical host (an r7 leg with the clean,
    recognizable hostname "prod-srv-002"), the derived eol_registry row must
    point at the canonical r7 Resource instead — not the vSphere VM's ugly
    display name — so a known, monitored host never again looks like an
    unmonitored coverage gap merely because of scan order."""
    r7_id = uuid.uuid4()
    vm_id = uuid.uuid4()
    with session_patcher(MODULE) as engine:
        with Session(engine) as s:
            r7_res = Resource(
                id=r7_id,
                domain="vuln",
                type="r7_asset",
                name="prod-srv-002",
                source="VulnAgent",
                zone="corpor",
            )
            vm_res = Resource(
                id=vm_id,
                domain="vsphere",
                type="vsphere_vm",
                name="prod-lnx-ca-001.example.internal (vcenter-01.corp.example.com)",
                source="VsphereAgent",
                zone="corpor",
            )
            s.add_all([r7_res, vm_res])
            s.flush()
            s.add(
                VsphereVm(
                    id=uuid.uuid4(),
                    resource_id=vm_id,
                    vcenter="vcenter-01.corp.example.com",
                    moref="vm-1",
                    name="prod-lnx-ca-001.example.internal",
                    guest_hostname="prod-lnx-ca-001.example.internal",
                    guest_full_name="CentOS 7 (64-bit)",
                )
            )
            # host_reconcile has ALREADY reconciled this host: same physical
            # machine, r7 leg carries the canonical short hostname.
            s.add(
                HostIdentity(
                    short_hostname="prod-lnx-ca-001",
                    r7_resource_id=r7_id,
                    vsphere_resource_id=vm_id,
                )
            )
            s.commit()

        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            row = s.query(EolRegistry).filter_by(asset_name="CentOS 7").one()
            assert row.resource_id == r7_id
            assert row.resource_id != vm_id


def test_derive_falls_back_to_raw_resource_when_host_not_yet_reconciled(session_patcher):
    """Before HostReconcileAgent has run for a brand-new host (no HostIdentity
    row exists yet), eol_registry still gets a resource_id — the raw
    per-domain Resource — exactly as before this fix. No regression for hosts
    host_reconcile hasn't reached yet."""
    with session_patcher(MODULE) as engine:
        _seed_inventory(engine)
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            centos = s.query(EolRegistry).filter_by(asset_name="CentOS 7").one()
            assert centos.resource_id is not None


def test_collect_returns_empty_when_no_inventory(session_patcher):
    with session_patcher(MODULE) as engine:  # empty DB
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            result = agent.collect()
            mock_tool.invoke.assert_not_called()
        assert result == []
        with Session(engine) as s:
            assert s.query(EolRegistry).count() == 0


def test_cycle_fetch_outage_does_not_null_known_eol_date(session_patcher):
    """H-4: a transient endoflife.date outage on a LATER run must not
    overwrite a previously-known-good eol_date/pci_risk_score fleet-wide.

    _write_eol_registry unconditionally set ``existing.eol_date = None`` for
    every product whose cycle fetch failed this run (the info dict simply
    never got an "eol" key), and ``_pci_risk_score(None, ...)`` then forced
    the risk score to 90 -- destroying a known-good value with no forensic
    trace and no error recorded, while the run still reported "completed".
    """
    with session_patcher(MODULE) as engine:
        _seed_inventory(engine)

        # First run: endoflife.date is healthy -> establishes a known-good
        # CentOS 7 eol_date/pci_risk_score.
        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _mock_cycles
            agent.collect()
            agent._write_eol_registry()

        with Session(engine) as s:
            before = s.query(EolRegistry).filter_by(asset_name="CentOS 7").one()
            assert before.eol_date is not None
            assert before.eol_date.date() == datetime(2024, 6, 30).date()
            assert before.pci_risk_score == 90  # 2024 EOL is in the past

        # Second run: endoflife.date is down for the "centos" product only.
        agent2 = _agent()

        def _flaky_cycles(payload, config=None):
            if payload["product"] == "centos":
                raise RuntimeError("simulated endoflife.date outage")
            return _mock_cycles(payload, config)

        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            mock_tool.invoke.side_effect = _flaky_cycles
            outcome = agent2.collect()
            agent2._write_eol_registry()

        # The outage must be visible on the run, not swallowed.
        assert isinstance(outcome, CollectOutcome)
        assert outcome.status == "partial"
        assert any("centos" in e.lower() for e in outcome.errors)

        with Session(engine) as s:
            after = s.query(EolRegistry).filter_by(asset_name="CentOS 7").one()
            assert after.eol_date is not None, (
                "a transient fetch failure must not null a known eol_date"
            )
            assert after.eol_date.date() == before.eol_date.date()
            assert after.pci_risk_score == before.pci_risk_score

        # Products whose fetch succeeded this run are still refreshed normally.
        with Session(engine) as s:
            rhel = s.query(EolRegistry).filter_by(asset_name="RHEL 8").one()
            assert rhel.eol_date.date() == datetime(2029, 5, 31).date()


def test_collect_reports_partial_when_nothing_in_batch_is_mappable(session_patcher):
    """TRK-134: inventory rows exist but NONE normalize (an under-scoped
    _OS_RULES) must surface as status="partial", not a silently-healthy-
    looking status="completed" with zero rows."""
    with session_patcher(MODULE) as engine:
        with Session(engine) as s:
            vm_res = Resource(
                id=uuid.uuid4(),
                domain="vsphere",
                type="vsphere_vm",
                name="vm-99",
                source="VsphereAgent",
                zone="corpor",
            )
            s.add(vm_res)
            s.flush()
            s.add(
                VsphereVm(
                    id=uuid.uuid4(),
                    resource_id=vm_res.id,
                    vcenter="vc1",
                    moref="vm-99",
                    name="vm-99",
                    guest_full_name="Some Weird Appliance OS 3.1",
                )
            )
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="linux",
                    type="linux_host",
                    name="appliance-2",
                    source="LinuxAgent",
                    zone="corpor",
                    metadata_={"distro": "AnotherUnknownDistro", "version": "5"},
                )
            )
            s.commit()

        agent = _agent()
        with patch(f"{MODULE}.eol_cycles_tool") as mock_tool:
            result = agent.collect()
            mock_tool.invoke.assert_not_called()

        assert isinstance(result, CollectOutcome)
        assert result.status == "partial"
        assert result.items == []
        assert result.count_override == 2  # 2 distinct unmapped OS strings
        assert len(result.errors) == 1
        assert "_OS_RULES" in result.errors[0]
        assert "2 distinct unmapped OS string(s) seen" in result.errors[0]


def test_write_details_surfaces_eol_failure(sqlite_engine):
    from contextlib import contextmanager

    from infra_brain.agents.base import CollectionResult

    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="eol",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _agent()
    result = CollectionResult(
        run_id=run_id, domain="eol", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated registry-write failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated registry-write failure" in e for e in result.errors)
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "failed"


# --- _suggest_migration helper -----------------------------------------------


def test_migration_suggestion_windows_server_2012_r2():
    assert _suggest_migration("Windows Server 2012 R2") == "Upgrade to Windows Server 2022"


def test_migration_suggestion_windows_server_2019():
    assert _suggest_migration("Windows Server 2019") == "Upgrade to Windows Server 2022"


def test_migration_suggestion_centos_7():
    assert _suggest_migration("CentOS 7") == "Migrate to Rocky Linux 9"


def test_migration_suggestion_centos_linux_7():
    assert _suggest_migration("CentOS Linux 7") == "Migrate to Rocky Linux 9"


def test_migration_suggestion_ubuntu_20_04():
    assert _suggest_migration("Ubuntu 20.04") == "Upgrade to Ubuntu 24.04 LTS"


def test_migration_suggestion_unknown_returns_none():
    assert _suggest_migration("Some Weird Appliance OS 2.0") is None
    assert _suggest_migration("") is None
    assert _suggest_migration("PostgreSQL 12") is None


# --- TRK-193 sub-bug 1: CentOS 6 must not resolve to the CentOS 7/8/9 target -


def test_migration_suggestion_centos_6_does_not_recommend_rocky_9():
    """The bare "centos" key used to catch CentOS 6 (no dedicated entry) and
    hand back the same "Migrate to Rocky Linux 9" suggestion as CentOS 7 --
    not a supported/sane direct hop. Explicitly assert the old bad answer is
    gone, independent of whatever the new answer says."""
    suggestion = _suggest_migration("CentOS 6")
    assert suggestion is not None
    # The old bug: CentOS 6 got the exact same suggestion string as CentOS 7.
    assert suggestion != "Migrate to Rocky Linux 9"


def test_migration_suggestion_centos_6_surfaces_human_decision_needed():
    """CentOS 6 now gets an explicit no-direct-path / human-decision result
    instead of a guessed-at concrete replacement target."""
    suggestion = _suggest_migration("CentOS 6")
    assert suggestion is not None
    lowered = suggestion.lower()
    assert "no supported direct migration path" in lowered
    assert "human migration decision" in lowered


def test_migration_suggestion_centos_linux_6_also_covered():
    suggestion = _suggest_migration("CentOS Linux 6")
    assert suggestion is not None
    assert suggestion != "Migrate to Rocky Linux 9"
    assert "no supported direct migration path" in suggestion.lower()


def test_migration_suggestion_centos_7_still_recommends_rocky_9():
    """Guard against an overcorrection: CentOS 7 (and other non-6 CentOS
    releases) must keep the existing Rocky Linux 9 recommendation."""
    assert _suggest_migration("CentOS 7") == "Migrate to Rocky Linux 9"
    assert _suggest_migration("CentOS 8") == "Migrate to Rocky Linux 9"
    assert _suggest_migration("CentOS 9") == "Migrate to Rocky Linux 9"
