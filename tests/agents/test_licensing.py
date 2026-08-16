"""Tests for LicensingAgent — entitlement vs. installed-base reconciliation
(GitLab issue #97).

Covers: success case (over- and under-entitlement findings from seeded
installed-software inventory + SoftwareLicense entitlement rows), empty result
(no entitlement rows seeded — graceful, not an exception), and exception
handling (installed-inventory read failure propagates rather than being
swallowed).

Graph-first P4 (2026-08-12) additionally pins the installed-base READ. The
agent used to count installs from ``HAS_SOFTWARE`` rows in the legacy
``resource_relationships`` store; it now counts them from the three inventory
fact tables. ``test_fact_table_read_equals_the_legacy_edge_derivation`` is the
load-bearing one: it reproduces ``graph_maintenance``'s HAS_SOFTWARE block
verbatim over a realistic fixture and asserts both paths yield the identical
``{product -> {host resource_id}}`` mapping.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.licensing import LicensingAgent
from infra_brain.etl.base import CollectorSkipped
from infra_brain.db.models import (
    ComplianceViolation,
    LinuxHost,
    LinuxPackage,
    R7Asset,
    R7Software,
    Resource,
    SoftwareLicense,
    WindowsSoftware,
)

MODULE = "infra_brain.agents.licensing"


def _agent():
    agent = LicensingAgent.__new__(LicensingAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def _make_asset_resource(session, name, domain="windows"):
    res = Resource(id=uuid.uuid4(), domain=domain, type=f"{domain}_host", name=name, source="test")
    session.add(res)
    session.flush()
    return res


# ``_make_software_resource`` / ``_link`` (the legacy-edge fixture builders)
# were deleted at the P5 integration with the oracle that was their only
# caller.


def _install_windows(session, host, name, version="1.0", publisher="ACME"):
    session.add(
        WindowsSoftware(
            id=uuid.uuid4(),
            resource_id=host.id,
            name=name,
            version=version,
            publisher=publisher,
        )
    )


def _install_r7(session, host, product, version="1.0", vendor="ACME"):
    """One R7Software row on the host's R7Asset (created once per host).

    ``r7_software`` is UNIQUE on (asset_id, product, version) and one asset
    carries ~74-140 software rows, so reusing the asset is what the real
    collector does — a fresh asset per package would be a fixture artefact
    that hides same-asset dedup behaviour.
    """
    asset = session.query(R7Asset).filter_by(resource_id=host.id).one_or_none()
    if asset is None:
        asset = R7Asset(id=uuid.uuid4(), resource_id=host.id, r7_asset_id=_next_int())
        session.add(asset)
        session.flush()
    session.add(
        R7Software(
            id=uuid.uuid4(),
            asset_id=asset.id,
            r7_asset_id=asset.r7_asset_id,
            product=product,
            version=version,
            vendor=vendor,
        )
    )
    return asset


def _install_linux(session, host, name, version="1.0", manager="apt"):
    lh = session.query(LinuxHost).filter_by(resource_id=host.id).one_or_none()
    if lh is None:
        lh = LinuxHost(
            id=uuid.uuid4(), resource_id=host.id, distro="ubuntu", kernel="6.8", arch="x86_64"
        )
        session.add(lh)
        session.flush()
    session.add(
        LinuxPackage(id=uuid.uuid4(), host_id=lh.id, name=name, version=version, manager=manager)
    )
    return lh


_counter = {"n": 0}


def _next_int() -> int:
    _counter["n"] += 1
    return _counter["n"]


# --- success: over- and under-entitlement findings --------------------------


def _seed_reconciliation_fixture(engine):
    """3 installs of "Over Product" (entitled 2 seats) + 1 install of
    "Under Product" (entitled 5 seats)."""
    with Session(engine) as s:
        for i in range(3):
            asset = _make_asset_resource(s, f"host-over-{i}")
            _install_windows(s, asset, "Over Product")

        asset = _make_asset_resource(s, "host-under-0")
        _install_windows(s, asset, "Under Product")

        s.add(
            SoftwareLicense(
                product="Over Product", license_type="per-seat", seats_entitled=2
            )
        )
        s.add(
            SoftwareLicense(
                product="Under Product", license_type="per-seat", seats_entitled=5
            )
        )
        s.commit()


def test_collect_and_write_reports_over_and_under_entitlement(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_reconciliation_fixture(engine)
        agent = _agent()
        items = agent.collect()
        assert len(items) == 2
        by_product = {i["data"]["product"]: i["data"] for i in items}
        assert by_product["Over Product"]["installed_count"] == 3
        assert by_product["Over Product"]["status"] == "over_entitlement"
        assert by_product["Under Product"]["installed_count"] == 1
        assert by_product["Under Product"]["status"] == "under_entitlement"

        agent._write_license_violations()

        with Session(engine) as s:
            violations = {v.host: v for v in s.query(ComplianceViolation).all()}
            assert violations["Over Product"].rule == "license_over_entitlement"
            assert violations["Over Product"].severity in ("medium", "high", "critical")
            assert violations["Over Product"].status == "open"
            assert violations["Under Product"].rule == "license_under_entitlement"
            assert violations["Under Product"].severity == "low"
            assert violations["Under Product"].status == "open"


def test_substring_product_match():
    """An entitlement seeded with a shorter product name still matches an
    installed software Resource whose product string is more specific
    (mirrors real-world "Microsoft SQL Server" vs. "Microsoft SQL Server 2019
    Standard")."""
    host_sets = {"microsoft sql server 2019 standard": {uuid.uuid4(), uuid.uuid4()}}
    count = LicensingAgent._match_installed_count("Microsoft SQL Server", host_sets)
    assert count == 2


def test_reconcile_one_in_compliance_when_counts_match():
    ent = SoftwareLicense(product="Exact Product", license_type="per-seat", seats_entitled=4)
    from datetime import datetime

    finding = LicensingAgent._reconcile_one(ent, 4, datetime.now(UTC))
    assert finding["status"] == "in_compliance"


def test_reconcile_one_unmetered_when_no_capacity_set():
    ent = SoftwareLicense(product="Site License Product", license_type="subscription")
    from datetime import datetime

    finding = LicensingAgent._reconcile_one(ent, 999, datetime.now(UTC))
    assert finding["status"] == "unmetered"


def test_write_license_violations_noop_when_no_findings(session_patcher):
    """An empty findings list (e.g. collect() returned early with no
    entitlements seeded) must not touch any existing violation row."""
    with session_patcher(MODULE) as engine:
        with Session(engine) as s:
            s.add(
                ComplianceViolation(
                    rule="license_over_entitlement",
                    host="Stale Product",
                    severity="high",
                    detail="stale",
                    status="open",
                )
            )
            s.commit()

        agent = _agent()
        agent._findings = []
        agent._write_license_violations()

        with Session(engine) as s:
            v = s.query(ComplianceViolation).filter_by(host="Stale Product").one()
            assert v.status == "open"  # _write_license_violations no-ops on empty findings


def test_write_license_violations_resolves_when_finding_clears(session_patcher):
    with session_patcher(MODULE) as engine:
        with Session(engine) as s:
            s.add(
                ComplianceViolation(
                    rule="license_over_entitlement",
                    host="Recovered Product",
                    severity="high",
                    detail="stale",
                    status="open",
                )
            )
            s.commit()

        agent = _agent()
        agent._findings = [{"status": "in_compliance", "product": "Recovered Product"}]
        agent._write_license_violations()

        with Session(engine) as s:
            v = s.query(ComplianceViolation).filter_by(host="Recovered Product").one()
            assert v.status == "resolved"


# --- empty result: no entitlement rows seeded --------------------------------


def test_collect_skips_when_no_entitlements_seeded(session_patcher):
    """Self-skip, not an empty success.

    This agent reconciles the installed base against operator-seeded
    entitlement ground truth; with no SoftwareLicense rows there is nothing to
    reconcile AGAINST. Returning [] recorded status="completed" with 0 rows,
    which the R3 monitor correctly reads as "silently empty?" — escalating 200x
    in a row for a reconciliation nobody had given any input to. Seeding one
    row clears the skip by itself.
    """
    with session_patcher(MODULE):  # empty DB, no SoftwareLicense rows
        agent = _agent()
        with pytest.raises(CollectorSkipped, match="no SoftwareLicense entitlement rows seeded"):
            agent.collect()
        assert agent._findings == []


# --- graph-first P4: the installed-base read ---------------------------------


# ``_legacy_edge_reader`` and ``_derive_has_software_like_graph_maintenance``
# — the frozen pre-P4 implementation and the deriver stub that fed it — were
# DELETED at the P5 integration. They were the two-commit equivalence oracle:
# frozen the day the agent was re-pointed onto the fact tables, kept green in
# CI from P4 until the P5 drop, which is exactly the service they owed. With
# ``resource_relationships`` gone they cannot even construct their inputs (the
# model is a non-mapped shim and ``create_all`` no longer builds the table).
# The oracle's non-vacuous behavioural claims — version collapse, case
# folding, NULL versions, key trimming, orphan exclusion — were assertions
# about the FACT-TABLE read all along and live on directly in
# ``test_fact_table_read_collapse_semantics`` below.


def _seed_realistic_installed_base(session) -> None:
    """A fixture with every shape the two readers could disagree on.

    * the same product on three hosts across all three sources;
    * two VERSIONS of one product on one host (must collapse to one host,
      not two — the version is not part of the licensing key). Seeded on the
      Linux side because ``windows_software`` is UNIQUE on (resource_id,
      name), so two versions of one product on one Windows host cannot exist;
    * mixed case and surrounding whitespace in the product string;
    * a product with a NULL/empty version (bare-name software node);
    * a Windows row and a Linux package that name the same product on the
      same host (one host, counted once);
    * a Rapid7 asset with NO ``resource_id`` (unlinked — must contribute
      nothing, exactly as the deriver's ``isnot(None)`` filter does).
    """
    win_host = _make_asset_resource(session, "win-01", domain="windows")
    lin_host = _make_asset_resource(session, "lin-01", domain="linux")
    r7_host = _make_asset_resource(session, "r7-01", domain="vuln")
    both_host = _make_asset_resource(session, "both-01", domain="linux")

    _install_windows(session, win_host, "Microsoft SQL Server 2019 Standard", "15.0")
    _install_windows(session, win_host, "Acme Widget", "2.0")
    _install_windows(session, win_host, "Bare Version Product", None)
    _install_windows(session, win_host, "  Padded Product  ", "3.0")

    _install_linux(session, lin_host, "nginx", "1.24.0", "apt")
    _install_linux(session, lin_host, "openssl", "3.0.2", "apt")
    # Two versions of one product on ONE host (apt + a hand-built copy).
    _install_linux(session, lin_host, "Acme Widget", "1.0", "apt")
    _install_linux(session, lin_host, "Acme Widget", "3.0", "snap")

    _install_r7(session, r7_host, "Microsoft SQL Server 2019 Standard", "15.0")
    _install_r7(session, r7_host, "ACME WIDGET", "2.0")

    # Same product named by two different collectors on the SAME host.
    _install_windows(session, both_host, "Shared Product", "1.0")
    _install_linux(session, both_host, "Shared Product", "1.0", "dpkg")

    # Unlinked Rapid7 asset — no resource_id, contributes nothing.
    orphan = R7Asset(id=uuid.uuid4(), resource_id=None, r7_asset_id=_next_int())
    session.add(orphan)
    session.flush()
    session.add(
        R7Software(
            id=uuid.uuid4(),
            asset_id=orphan.id,
            r7_asset_id=orphan.r7_asset_id,
            product="Orphaned Product",
            version="1.0",
        )
    )
    session.flush()


def test_fact_table_read_collapse_semantics(sqlite_engine):
    """The behavioural half of the retired P4 equivalence test.

    Until P5 this test derived HAS_SOFTWARE edges with a frozen copy of the
    pre-P4 deriver and asserted the fact-table read matched the edge read.
    The store the oracle needed is dropped, so the equivalence claim is
    retired (proven and CI-held for its whole window); what must NOT retire
    are the collapse semantics it also pinned, restated here directly against
    ``LicensingAgent._installed_host_sets_by_product``:
    """
    with Session(sqlite_engine) as s:
        _seed_realistic_installed_base(s)
        s.commit()

    with Session(sqlite_engine) as s:
        from_facts = LicensingAgent._installed_host_sets_by_product(s)

    assert len(from_facts["acme widget"]) == 3, (
        "win-01 (one version) + lin-01 (TWO versions, collapsed) + r7-01 (different case) "
        "= 3 distinct hosts"
    )
    assert len(from_facts["shared product"]) == 1, "windows + linux naming one host = 1 host"
    assert "bare version product" in from_facts, "NULL version must still be counted"
    assert "padded product" in from_facts, "product keys are trimmed"
    assert "orphaned product" not in from_facts, "an asset with no resource_id contributes nothing"


def test_installed_base_counted_without_any_graph_derivation(sqlite_engine):
    """The installed base is read FIRST-HAND from the collectors' own tables.

    Before P4 an install only counted once ``graph_maintenance``'s 2-hourly
    pass had materialised a ``software`` Resource and emitted a HAS_SOFTWARE
    edge for it, so a freshly-collected host was invisible to license
    reconciliation until then. Here nothing has been derived at all: there are
    no ``domain="software"`` Resources and no ``resource_relationships`` rows.
    """
    with Session(sqlite_engine) as s:
        _seed_realistic_installed_base(s)
        s.commit()

    with Session(sqlite_engine) as s:
        # P5: the legacy store does not exist at all — stronger than count()==0.
        from sqlalchemy import inspect as _sqla_inspect

        assert "resource_relationships" not in _sqla_inspect(s.get_bind()).get_table_names()
        assert s.query(Resource).filter_by(domain="software").count() == 0
        host_sets = LicensingAgent._installed_host_sets_by_product(s)

    assert host_sets["nginx"], "a Linux package must count without any graph derivation"
    assert len(host_sets["acme widget"]) == 3
    assert len(host_sets["microsoft sql server 2019 standard"]) == 2  # windows + rapid7 host


# --- exception handling: installed-inventory read failure -------------------


def test_collect_raises_when_installed_inventory_read_fails(sqlite_engine):
    with Session(sqlite_engine) as s:
        s.add(
            SoftwareLicense(product="Any Product", license_type="per-seat", seats_entitled=1)
        )
        s.commit()

    call_count = {"n": 0}

    @contextmanager
    def _get_session():
        call_count["n"] += 1
        if call_count["n"] == 1:
            with Session(sqlite_engine) as s:
                yield s
        else:
            raise RuntimeError("simulated inventory read failure")

    agent = _agent()
    with patch(f"{MODULE}.get_session", _get_session):
        with pytest.raises(RuntimeError, match="simulated inventory read failure"):
            agent.collect()
