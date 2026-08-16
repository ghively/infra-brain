"""C-1 regression: a transient per-asset fetch failure must never auto-close that
asset's entire open-CVE set.

The bug: ``VulnAgent._fetch_asset_vulns`` swallowed every exception and returned
``(asset_id, [])`` — indistinguishable from "Rapid7 reports zero findings for this
host". ``_write_vuln_queue``'s FE-9 reconciliation pass then built its scope from
ALL of ``fetch_ids`` (including the failed ones), so every open ``vuln_queue`` row
on that host whose ``(resource_id, cve_id)`` was missing from ``seen_keys`` — i.e.
all of them — was flipped to ``CLOSED_AUTO``. One Rapid7 500/timeout silently
"remediated" a whole host, the dashboard's "Open CVEs" count dropped, and the run
still reported ``completed`` with no errors.

These tests exercise the REAL failure mode (the Rapid7 tool raising inside the
thread-pool fan-out), not the shape of the fix.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.base import CollectionResult
from infra_brain.agents.vuln import VulnAgent
from infra_brain.db.models import CollectionRun, Resource, VulnQueueItem

MODULE = "infra_brain.agents.vuln"


def _feeder_agent(cap=750):
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_asset_cap = cap
    agent.settings.rapid7_vuln_rotation_slots = 0
    agent.callbacks = []
    return agent


def _seed_asset(engine, name):
    with Session(engine) as s:
        res = Resource(
            id=uuid.uuid4(),
            domain="vuln",
            type="r7_asset",
            name=name,
            source="VulnAgent",
            zone="corpor",
        )
        s.add(res)
        s.commit()
        return res.id


def _asset(asset_id, name, risk=100, vulns=2):
    return {
        "id": asset_id,
        "hostName": name,
        "riskScore": risk,
        "vulnerabilities": {"total": vulns},
    }


def _statuses(engine):
    with Session(engine) as s:
        return {r.cve_id: r.status for r in s.query(VulnQueueItem).all()}


def test_transient_fetch_failure_does_not_auto_close_that_assets_cves(session_patcher):
    """THE bug. srv-1's fetch raises on the second run; srv-2's succeeds and
    legitimately drops a CVE.

    srv-1's CVE must stay OPEN (nothing was observed for it, so nothing can be
    proven remediated). srv-2's dropped CVE must still be auto-closed — the fix
    must not disable reconciliation wholesale, only narrow it to proven-observed
    assets.
    """
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, "srv-1")
        _seed_asset(engine, "srv-2")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-1", risk=300), _asset(2, "srv-2", risk=200)]

        first_pass = {
            1: {"resources": [{"id": "CVE-2026-1111"}, {"id": "CVE-2026-1112"}]},
            2: {"resources": [{"id": "CVE-2026-2221"}, {"id": "CVE-2026-2222"}]},
        }

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = lambda payload, config=None: first_pass[
                payload["asset_id"]
            ]
            agent._write_vuln_queue()

        assert _statuses(engine) == {
            "CVE-2026-1111": "open",
            "CVE-2026-1112": "open",
            "CVE-2026-2221": "open",
            "CVE-2026-2222": "open",
        }

        # Second run: Rapid7 500s on srv-1 (asset_id=1). srv-2 answers fine but
        # no longer reports CVE-2026-2222 (genuinely remediated upstream).
        def _flaky(payload, config=None):
            if payload["asset_id"] == 1:
                raise RuntimeError("Rapid7 API returned HTTP 500 for /assets/1/vulnerabilities")
            return {"resources": [{"id": "CVE-2026-2221"}]}

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = _flaky
            agent._write_vuln_queue()

        statuses = _statuses(engine)
        assert statuses["CVE-2026-1111"] == "open", (
            "srv-1's fetch FAILED — its CVEs were never observed as absent, so "
            "auto-closing them fabricates a remediation that never happened"
        )
        assert statuses["CVE-2026-1112"] == "open", (
            "the whole host's open-CVE set must survive a transient fetch failure"
        )
        assert statuses["CVE-2026-2221"] == "open", "still reported by srv-2 — stays open"
        assert statuses["CVE-2026-2222"] == "resolved", (
            "srv-2 WAS successfully observed and no longer reports this CVE — "
            "reconciliation must still close it (the fix narrows the scope, it "
            "does not disable reconciliation)"
        )


def test_every_asset_failing_closes_nothing_at_all(session_patcher):
    """Total Rapid7 outage during the detail phase: zero assets are proven
    observed, so the reconciliation pass must be a complete no-op rather than
    wiping the entire open-CVE queue."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, "srv-1")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-1")]

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {"resources": [{"id": "CVE-2026-3333"}]}
            agent._write_vuln_queue()
        assert _statuses(engine) == {"CVE-2026-3333": "open"}

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = TimeoutError("read timeout")
            agent._write_vuln_queue()

        assert _statuses(engine) == {"CVE-2026-3333": "open"}


def test_fetch_failure_surfaces_as_partial_not_completed(sqlite_engine):
    """The run must not report a clean ``completed``/no-errors. The failure is
    reported through the EXISTING CollectionRun status vocabulary — "partial",
    the same literal ``CollectOutcome.status`` produces for errors-plus-data."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vuln",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        res = Resource(
            id=uuid.uuid4(),
            domain="vuln",
            type="r7_asset",
            name="srv-1",
            source="VulnAgent",
            zone="corpor",
        )
        s.add(res)
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _feeder_agent()
    agent._last_assets = [_asset(1, "srv-1")]
    result = CollectionResult(
        run_id=run_id, domain="vuln", resources_found=1, drift_count=0, status="completed"
    )

    with (
        patch(f"{MODULE}.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool,
    ):
        mock_tool.invoke.side_effect = RuntimeError("Rapid7 API returned HTTP 500")
        agent._write_vuln_queue(result)

    assert result.status == "partial", (
        "a swallowed per-asset fetch failure must degrade the run to partial, "
        "never leave it reporting a clean completed"
    )
    assert any("Rapid7 API returned HTTP 500" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "partial"
        assert "Rapid7 API returned HTTP 500" in (run.error_message or "")


def test_clean_run_stays_completed_and_reports_no_errors(sqlite_engine):
    """No fetch failures -> the run status and error list are untouched."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vuln",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="vuln",
                type="r7_asset",
                name="srv-1",
                source="VulnAgent",
                zone="corpor",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _feeder_agent()
    agent._last_assets = [_asset(1, "srv-1")]
    result = CollectionResult(
        run_id=run_id, domain="vuln", resources_found=1, drift_count=0, status="completed"
    )

    with (
        patch(f"{MODULE}.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool,
    ):
        mock_tool.invoke.return_value = {"resources": [{"id": "CVE-2026-4444"}]}
        agent._write_vuln_queue(result)

    assert result.status == "completed"
    assert result.errors == []
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "completed"


def test_partial_never_upgrades_an_already_failed_run(sqlite_engine):
    """A run already marked failed (e.g. by an earlier detail-write phase) must
    not be softened to "partial" by a later fetch failure."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vuln",
                trigger_type="scheduled",
                trigger_source="all",
                status="failed",
            )
        )
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="vuln",
                type="r7_asset",
                name="srv-1",
                source="VulnAgent",
                zone="corpor",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _feeder_agent()
    agent._last_assets = [_asset(1, "srv-1")]
    result = CollectionResult(
        run_id=run_id,
        domain="vuln",
        resources_found=1,
        drift_count=0,
        status="failed",
        errors=["earlier phase blew up"],
    )

    with (
        patch(f"{MODULE}.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool,
    ):
        mock_tool.invoke.side_effect = RuntimeError("Rapid7 API returned HTTP 500")
        agent._write_vuln_queue(result)

    assert result.status == "failed"
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "failed"


def test_detail_writer_wiring_carries_the_result_through(sqlite_engine):
    """The reporting half only works if ``_detail_writers()`` actually binds the
    run's ``CollectionResult`` to the feeder phase. Exercise the real wiring
    (``_detail_writers`` -> zero-arg callable -> ``_write_details``) rather than
    calling ``_write_vuln_queue`` by hand, so a regression in the binding is
    caught."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vuln",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="vuln",
                type="r7_asset",
                name="srv-1",
                source="VulnAgent",
                zone="corpor",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _feeder_agent()
    agent._last_assets = [_asset(1, "srv-1")]
    result = CollectionResult(
        run_id=run_id, domain="vuln", resources_found=1, drift_count=0, status="completed"
    )

    writers = agent._detail_writers("all", result)
    feeder = writers[1]  # phase 2 — the vuln_queue feeder

    with (
        patch(f"{MODULE}.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool,
    ):
        mock_tool.invoke.side_effect = RuntimeError("Rapid7 API returned HTTP 503")
        agent._write_details(result, feeder)

    assert result.status == "partial"
    assert any("HTTP 503" in e for e in result.errors)
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "partial"


def test_characterization_closed_auto_row_is_never_reopened(session_patcher):
    """CHARACTERIZATION (documents current behaviour, does NOT endorse it).

    Answers "does existing C-1 damage self-heal?" — it does NOT. Once a row is
    flipped to ``CLOSED_AUTO``, a later SUCCESSFUL run that still sees the CVE
    refreshes ``last_updated``/``severity`` but leaves ``status`` alone
    (``_upsert_vuln_row``'s existing-row path, "Do NOT overwrite status"), and
    the reconciliation pass only ever queries rows already in
    ``OPEN_VULN_STATUSES``. So the row stays "resolved" forever and the
    dashboard's "Open CVEs" undercount is permanent until backfilled.

    This test exists so that if a future re-open path is added, this assertion
    fails loudly and is updated deliberately rather than by accident.
    """
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, "srv-1")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-1")]

        # Run 1: CVE seen -> open.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {"resources": [{"id": "CVE-2026-7777"}]}
            agent._write_vuln_queue()

        # Simulate the damage a pre-fix run inflicted.
        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            row.status = "resolved"
            s.commit()

        # Run 2 succeeds and Rapid7 STILL reports the CVE on this host.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {"resources": [{"id": "CVE-2026-7777"}]}
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert row.status == "resolved", (
                "documented, unfixed: a re-confirmed CVE does not re-open a "
                "CLOSED_AUTO row — existing damage does not self-heal"
            )
            assert row.last_updated is not None, "the row IS still being touched each run"


@pytest.mark.parametrize("exc", [RuntimeError("boom"), TimeoutError(), ValueError("bad json")])
def test_any_exception_type_from_the_rapid7_tool_protects_the_scope(session_patcher, exc):
    """Not just the exception type the author had in mind: ANY exception escaping
    the tool must protect the asset from reconciliation."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, "srv-1")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-1")]

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {"resources": [{"id": "CVE-2026-5555"}]}
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            row.last_updated = datetime.now(UTC)
            s.commit()

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = exc
            agent._write_vuln_queue()

        assert _statuses(engine) == {"CVE-2026-5555": "open"}
