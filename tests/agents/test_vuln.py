"""Tests for VulnAgent — I10 coverage."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.base import CollectorSkipped
from infra_brain.agents.vuln import VulnAgent
from infra_brain.db.models import (
    CollectionRun,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetSite,
    R7AssetUser,
    R7Site,
    R7Software,
    R7Solution,
    R7Tag,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    Resource,
    VulnQueueItem,
)


def _mock_settings(rapid7_url="http://r7.example.com", rapid7_api_key="key"):
    s = MagicMock()
    s.rapid7_url = rapid7_url
    s.rapid7_api_key = rapid7_api_key
    s.rapid7_username = "api_key_auth"
    s.rapid7_ssl_verify = False
    s.api_timeout_seconds = 30
    s.default_zone = "corpor"
    return s


def test_vuln_agent_collect_raises_collector_skipped_when_unconfigured():
    """When rapid7_url is empty, collect() raises CollectorSkipped (status='skipped'),
    not a silent empty-but-successful run."""
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = _mock_settings(rapid7_url="", rapid7_api_key="")
    agent.callbacks = []

    with (
        patch("infra_brain.agents.vuln._unconfigured", return_value=True),
        pytest.raises(CollectorSkipped),
    ):
        agent.collect("all")


def test_vuln_agent_collect_returns_only_assets():
    """collect() returns r7_asset items and no longer pulls the unbounded global
    vuln catalog (that blew past collect_timeout). The asset list is stashed for
    the detail-write phase."""
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = _mock_settings()
    agent.callbacks = []

    asset_page = [
        {
            "id": 1,
            "hostName": "srv-01",
            "ip": "10.0.0.1",
            "riskScore": 500,
            # os is a STRING (the bug fix) and osFingerprint is the dict.
            "os": "Microsoft Windows Server 2019 Datacenter Edition 1809",
            "osFingerprint": {
                "family": "Windows",
                "product": "Windows Server 2019 Datacenter Edition",
                "version": "1809",
                "vendor": "Microsoft",
                "architecture": "x86_64",
            },
            "vulnerabilities": {"total": 3, "critical": 1, "severe": 2},
        }
    ]

    with (
        patch("infra_brain.agents.vuln._unconfigured", return_value=False),
        patch("infra_brain.agents.vuln.rapid7_assets_all_tool") as mock_assets,
    ):
        mock_assets.invoke.return_value = asset_page
        result = agent.collect("all")

    assets = [r for r in result if r["type"] == "r7_asset"]
    vulns = [r for r in result if r["type"] == "r7_vulnerability"]
    assert len(assets) == 1
    assert assets[0]["name"] == "srv-01"
    assert assets[0]["data"]["risk_score"] == 500
    # OS bug fix: os is the STRING (not "" from the old dict-only path).
    assert assets[0]["data"]["os"] == "Microsoft Windows Server 2019 Datacenter Edition 1809"
    assert assets[0]["data"]["os_family"] == "Windows"
    assert assets[0]["data"]["os_product"] == "Windows Server 2019 Datacenter Edition"
    # vulnerabilities count comes from the dict's total.
    assert assets[0]["data"]["vulnerabilities"] == 3
    # The unbounded global vuln-catalog pull is gone — no r7_vulnerability rows.
    assert vulns == []
    # collect() stashes the raw asset list for the vuln_queue feeder.
    assert agent._last_assets == asset_page


def test_vuln_agent_run_returns_collection_result():
    """run() wraps collect() and returns a CollectionResult."""
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = _mock_settings(rapid7_url="", rapid7_api_key="")
    agent.domain = "vuln"
    agent.callbacks = []

    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    with (
        patch("infra_brain.etl.base.get_session") as mock_get_session,
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.vuln._unconfigured", return_value=True),
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        with patch("infra_brain.agents.base.get_chat_model"):
            from infra_brain.agents.base import BaseAgent

            result = BaseAgent.run(agent, trigger_type="scheduled", scope="all")

    from infra_brain.agents.base import CollectionResult

    assert isinstance(result, CollectionResult)
    assert result.domain == "vuln"


# ---------------------------------------------------------------------------
# vuln_queue feeder (MR3) — DB-backed via shared sqlite fixtures
# ---------------------------------------------------------------------------

MODULE = "infra_brain.agents.vuln"
BRIDGE_MODULE = "infra_brain.agents.vuln_cve"


def _feeder_agent(cap=750):
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_asset_cap = cap
    agent.callbacks = []
    return agent


def _seed_asset(engine, asset_id, name):
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


def _seed_r7_vuln(engine, r7_vuln_id, severity=None, cvss_v3_score=None):
    """Seed (or upsert) an ``r7_vulnerabilities`` enrichment row so
    ``_write_vuln_queue``'s slug -> severity lookup (GL-122 fix) resolves it.
    Real Rapid7 asset-vuln findings never carry severity/CVSS themselves —
    only the separate definition endpoint (VulnCveBridge) does; this is the
    enrichment source the fixed feeder now joins against."""
    with Session(engine) as s:
        existing = s.query(R7Vulnerability).filter_by(r7_vuln_id=r7_vuln_id).first()
        if existing is not None:
            existing.severity = severity
            existing.cvss_v3_score = cvss_v3_score
        else:
            s.add(
                R7Vulnerability(
                    id=uuid.uuid4(),
                    r7_vuln_id=r7_vuln_id,
                    severity=severity,
                    cvss_v3_score=cvss_v3_score,
                )
            )
        s.commit()


def test_vuln_queue_upserts_rows_with_correct_sla(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        # GL-122 fix: severity comes from the r7_vulnerabilities enrichment
        # join, not the asset-vuln payload (which never carries it in
        # production) — seed the enrichment rows the feeder now looks up.
        _seed_r7_vuln(engine, "CVE-2021-1001", severity="Critical")
        _seed_r7_vuln(engine, "CVE-2021-1002", severity="High")
        _seed_r7_vuln(engine, "CVE-2021-1003", severity="Moderate")
        _seed_r7_vuln(engine, "CVE-2021-1004", cvss_v3_score=2.0)  # no severity → low via cvss
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        vulns = {
            "resources": [
                {"id": "CVE-2021-1001", "severity": "Critical"},
                {"id": "CVE-2021-1002", "severity": "High"},
                {"id": "CVE-2021-1003", "severity": "Moderate"},
                {"id": "CVE-2021-1004", "cvssV3Score": 2.0},  # severity missing → low via cvss
            ]
        }
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = vulns
            agent._write_vuln_queue()

        with Session(engine) as s:
            rows = {r.cve_id: r for r in s.query(VulnQueueItem).all()}
            assert set(rows) == {"CVE-2021-1001", "CVE-2021-1002", "CVE-2021-1003", "CVE-2021-1004"}
            assert rows["CVE-2021-1001"].severity == "critical"
            assert rows["CVE-2021-1002"].severity == "high"
            assert rows["CVE-2021-1003"].severity == "medium"  # Moderate → medium
            assert rows["CVE-2021-1004"].severity == "low"
            # SLA windows: critical=3, high=7, medium=30, low=90 days
            span = (rows["CVE-2021-1001"].sla_due - rows["CVE-2021-1001"].last_updated).days
            assert span == 3
            assert (rows["CVE-2021-1002"].sla_due - rows["CVE-2021-1002"].last_updated).days == 7
            assert (rows["CVE-2021-1003"].sla_due - rows["CVE-2021-1003"].last_updated).days == 30
            assert (rows["CVE-2021-1004"].sla_due - rows["CVE-2021-1004"].last_updated).days == 90
            assert all(r.status == "open" for r in rows.values())


def test_vuln_queue_idempotent_and_preserves_status(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_r7_vuln(engine, "CVE-2021-9999", severity="Low")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {"resources": [{"id": "CVE-2021-9999", "severity": "Low"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        # Human triages the row → "remediated"
        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            row.status = "remediated"
            s.commit()

        # Rapid7 re-enriches the vuln definition with a higher severity.
        _seed_r7_vuln(engine, "CVE-2021-9999", severity="Critical")

        # Re-run with the same finding for the same CVE.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {
                "resources": [{"id": "CVE-2021-9999", "severity": "Critical"}]
            }
            agent._write_vuln_queue()

        with Session(engine) as s:
            rows = s.query(VulnQueueItem).all()
            assert len(rows) == 1  # idempotent — no duplicate
            assert rows[0].status == "remediated"  # status preserved
            assert rows[0].severity == "critical"  # severity refreshed


def test_vuln_queue_reconciliation_closes_stale_open_row(session_patcher):
    """FE-9: a CVE that Rapid7 no longer reports for an asset it just
    re-scanned must be auto-closed (status -> resolved), not left "open"
    forever. This is the root cause of "Open CVEs" over-counting already
    -remediated findings."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {
            "resources": [
                {"id": "CVE-2021-1111", "severity": "High"},
                {"id": "CVE-2021-2222", "severity": "Low"},
            ]
        }
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            rows = {r.cve_id: r for r in s.query(VulnQueueItem).all()}
            assert rows["CVE-2021-1111"].status == "open"
            assert rows["CVE-2021-2222"].status == "open"

        # Re-scan the SAME asset: Rapid7 now only reports CVE-2021-1111 —
        # CVE-2021-2222 has been remediated upstream and dropped from the
        # asset's finding list.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {
                "resources": [{"id": "CVE-2021-1111", "severity": "High"}]
            }
            agent._write_vuln_queue()

        with Session(engine) as s:
            rows = {r.cve_id: r for r in s.query(VulnQueueItem).all()}
            assert rows["CVE-2021-1111"].status == "open", "still reported — stays open"
            assert rows["CVE-2021-2222"].status == "resolved", (
                "no longer reported by a re-scan of the same asset — must be auto-closed"
            )


def test_vuln_queue_reconciliation_never_touches_human_set_status(session_patcher):
    """A row a human/remediation flow moved to a closed-but-not-CLOSED_AUTO
    state (e.g. accepted_risk) must never be flipped by the reconciliation
    pass — it's already not "open" and its provenance (a human decision, not
    "Rapid7 stopped reporting it") must be preserved."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {
                "resources": [{"id": "CVE-2021-3333", "severity": "Low"}]
            }
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            row.status = "accepted_risk"
            s.commit()

        # Re-scan: Rapid7 no longer reports this CVE for the asset.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {"resources": []}
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert row.status == "accepted_risk", "human-set status must never be auto-overwritten"


def test_vuln_queue_reconciliation_scoped_to_assets_scanned_this_run(session_patcher):
    """The reconciliation pass must never close rows for an asset outside
    this run's bounded/capped batch — only assets actually re-fetched."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-1")
        _seed_asset(engine, 2, "srv-2")
        agent = _feeder_agent(cap=1)  # only srv-1 (higher risk) gets scanned
        agent._last_assets = [
            _asset(1, "srv-1", risk=300),
            _asset(2, "srv-2", risk=100),
        ]

        def _fetch(payload, config=None):
            return {"resources": [{"id": f"CVE-2026-900{payload['asset_id']}", "severity": "Low"}]}

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = _fetch
            agent._write_vuln_queue()  # seeds CVE-2026-9001 on srv-1 only (cap=1)

        # Directly seed a row on srv-2 (the asset the cap skipped this run),
        # simulating a CVE recorded on a previous, uncapped pass.
        with Session(engine) as s:
            srv2 = s.query(Resource).filter_by(name="srv-2").one()
            s.add(
                VulnQueueItem(
                    resource_id=srv2.id,
                    cve_id="CVE-2020-0001",
                    severity="low",
                    status="open",
                    last_updated=datetime.now(UTC),
                )
            )
            s.commit()

        # Re-run: cap=1 again means srv-2 is skipped again this run too.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.side_effect = _fetch
            agent._write_vuln_queue()

        with Session(engine) as s:
            srv2_row = s.query(VulnQueueItem).filter_by(cve_id="CVE-2020-0001").one()
            assert srv2_row.status == "open", (
                "srv-2 was never re-scanned this run (cap skipped it) — its row must "
                "not be auto-closed just because it wasn't in this run's seen_keys"
            )


def test_vuln_queue_respects_cap(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        # 3 assets with vulns, all with Resources; cap=2 keeps top-2 by risk.
        for i, risk in ((1, 300), (2, 200), (3, 100)):
            _seed_asset(engine, i, f"srv-{i}")
        agent = _feeder_agent(cap=2)
        agent._last_assets = [
            _asset(1, "srv-1", risk=300),
            _asset(2, "srv-2", risk=200),
            _asset(3, "srv-3", risk=100),
        ]
        seen = []

        def _fetch(payload, config=None):
            seen.append(payload["asset_id"])
            return {"resources": [{"id": f"CVE-2026-100{payload['asset_id']}", "severity": "Low"}]}

        with caplog.at_level(logging.INFO, logger=MODULE):
            with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
                mock_tool.invoke.side_effect = _fetch
                agent._write_vuln_queue()

        # Only the top-2 (risk 300, 200 → asset ids 1,2) were fetched.
        assert sorted(seen) == [1, 2]
        with Session(engine) as s:
            assert s.query(VulnQueueItem).count() == 2
        assert any("cap=2" in r.message and "1 skipped" in r.message for r in caplog.records)


def test_vuln_queue_skips_assets_without_vulns(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-clean")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-clean", vulns=0)]  # 0 vulns → excluded
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            agent._write_vuln_queue()
            mock_tool.invoke.assert_not_called()
        with Session(engine) as s:
            assert s.query(VulnQueueItem).count() == 0


def test_vuln_queue_severity_from_enrichment_not_asset_vuln_payload(session_patcher):
    """GL-122 regression test: a CVSS 8.0 finding must land as "high", not
    "low". The Rapid7 asset-vuln item itself never carries severity/CVSS in
    production — only the enriched r7_vulnerabilities row (keyed by the same
    slug) does. Before the fix, ``_severity_band`` was fed the asset-vuln
    item's (always-empty) severity/cvss fields and unconditionally fell
    through to "low" regardless of the finding's real CVSS."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        slug = "CVE-2026-8080"
        # The real severity data: CVSS 8.0 -> "high" per the threshold table
        # (critical >=9.0, high >=7.0, medium >=4.0, else low).
        _seed_r7_vuln(engine, slug, cvss_v3_score=8.0)
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        # The asset-vuln payload carries NO severity/cvss field at all — this
        # mirrors the real Rapid7 asset-vulnerabilities endpoint shape
        # ({id, instances, results, since, status}), not a test artifact.
        payload = {"resources": [{"id": slug, "instances": 1, "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id=slug).one()
            assert row.severity == "high"
            # SLA window for "high" is 7 days.
            assert (row.sla_due - row.last_updated).days == 7


def test_vuln_queue_severity_falls_back_to_low_when_not_yet_enriched(session_patcher):
    """A freshly-discovered CVE with no r7_vulnerabilities enrichment row yet
    (enrichment runs on a lag — docs/agents/vuln.md) must still land as "low"
    — an explicit "not yet enriched" placeholder, same behavior as before the
    fix for this specific case."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        slug = "CVE-2026-9090"
        # No _seed_r7_vuln call — this slug has no enrichment row at all.
        payload = {"resources": [{"id": slug, "instances": 1, "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id=slug).one()
            assert row.severity == "low"
            assert (row.sla_due - row.last_updated).days == 90


# ---------------------------------------------------------------------------
# SLA anchoring + immutability (GitLab #136)
# ---------------------------------------------------------------------------


def _utc_naive(dt):
    """Normalize an aware/naive datetime to naive UTC (sqlite drops tzinfo)."""

    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def test_vuln_queue_rescan_does_not_reset_sla_due(session_patcher):
    """GitLab #136 regression: a rescan re-confirming an already-seen finding
    must refresh last_updated ONLY — sla_due is immutable. The old code
    recomputed ``sla_due = now + window`` on every upsert, so the daily
    Rapid7 rescan reset the SLA clock and no High/Medium CVE on a rescanned
    asset could ever breach its deadline."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_r7_vuln(engine, "CVE-2026-4001", severity="Severe")  # → high, 7d window
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {"resources": [{"id": "CVE-2026-4001", "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        # Simulate a row that has been open for a while: backdate sla_due so a
        # reset (old bug) would be unmistakable.
        backdated = datetime.now(UTC) - timedelta(days=10)
        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            row.sla_due = backdated
            row.last_updated = backdated
            s.commit()

        # Daily rescan re-reports the exact same finding.
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert _utc_naive(row.sla_due) == _utc_naive(backdated), (
                "sla_due must be IMMUTABLE on rescan — the deadline was extended"
            )
            assert _utc_naive(row.last_updated) > _utc_naive(backdated), (
                "last_updated must still be refreshed by the rescan"
            )
            # The overdue row is now actually reportable as an SLA breach.
            assert _utc_naive(row.sla_due) < _utc_naive(datetime.now(UTC))


def test_vuln_queue_new_row_anchors_sla_on_rapid7_since(session_patcher):
    """A NEW row's sla_due derives from the finding's first-seen date
    (Rapid7's per-finding ``since``), not from the insert time — a finding
    already open upstream for 40 days with a 7-day window is overdue on
    arrival, not granted a fresh full window."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_r7_vuln(engine, "CVE-2026-4002", severity="Severe")  # → high, 7d window
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        since = datetime.now(UTC) - timedelta(days=40)
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload = {
            "resources": [{"id": "CVE-2026-4002", "status": "vulnerable", "since": since_str}]
        }
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            expected = _utc_naive(since + timedelta(days=7))
            assert abs((_utc_naive(row.sla_due) - expected).total_seconds()) < 1, (
                "sla_due must anchor on the Rapid7 ``since`` first-seen date"
            )
            assert _utc_naive(row.sla_due) < _utc_naive(datetime.now(UTC)), (
                "a finding 40 days old with a 7-day window is overdue on arrival"
            )


def test_vuln_queue_unenriched_rescan_does_not_downgrade_or_extend(session_patcher):
    """A rescan that runs before enrichment (band placeholder = low) must not
    downgrade a previously enriched severity, and must not stretch the
    deadline to the lenient 90-day low window."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_r7_vuln(engine, "CVE-2026-4003", severity="Critical")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {"resources": [{"id": "CVE-2026-4003", "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            original_due = _utc_naive(s.query(VulnQueueItem).one().sla_due)

        # Enrichment row vanishes (e.g. wiped/re-syncing) — rescan now sees the
        # slug as not-yet-enriched and would have written the "low" placeholder.
        with Session(engine) as s:
            s.query(R7Vulnerability).delete()
            s.commit()

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert row.severity == "critical", "placeholder 'low' must not downgrade enrichment"
            assert _utc_naive(row.sla_due) == original_due, "deadline must not move"


def test_vuln_queue_severity_upgrade_tightens_sla_never_extends(session_patcher):
    """When enrichment lands and upgrades the low placeholder to critical, the
    deadline recomputes from the first-seen anchor with the stricter window —
    it tightens (earlier) and can never move later."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        # First pass: no enrichment row yet → low placeholder, 90-day window.
        payload = {"resources": [{"id": "CVE-2026-4004", "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            first = s.query(VulnQueueItem).one()
            assert first.severity == "low"
            low_due = _utc_naive(first.sla_due)
            anchor = low_due - timedelta(days=90)

        # Enrichment lands: the vuln is actually Critical (3-day window).
        _seed_r7_vuln(engine, "CVE-2026-4004", severity="Critical")
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert row.severity == "critical"
            new_due = _utc_naive(row.sla_due)
            assert new_due < low_due, "an upgrade must tighten the deadline"
            assert abs((new_due - (anchor + timedelta(days=3))).total_seconds()) < 1, (
                "the tightened deadline recomputes from the original first-seen anchor"
            )


# ---------------------------------------------------------------------------
# Same-run severity reconciliation (GitLab #173 / #188 Bug 3)
# ---------------------------------------------------------------------------


def test_vuln_queue_severity_reconciled_same_run_after_enrichment(session_patcher):
    """GitLab #173 / #188 Bug 3 regression.

    Root cause confirmed by reading the code: ``_write_vuln_queue`` writes a
    "low" placeholder severity for any CVE whose slug has no
    ``r7_vulnerabilities`` enrichment row YET. ``_write_vuln_details`` (the
    MR7 enrichment phase) then upserts that very enrichment row LATER in the
    SAME collection run (``_detail_writers`` runs phase 2 before phase 3) —
    but, before this fix, nothing ever revisited the vuln_queue row phase 2
    had already written. So a CVE that got fully enriched within a single
    run still reported "low" in vuln_queue until some LATER run happened to
    re-upsert the same host (i.e. the host was selected again inside the
    750-asset cap in ``_bounded_assets``) — exactly the "only upgrades on
    re-upsert" mechanism #173 tracks.

    This test runs the real phase order for one host/CVE in a single
    simulated collection run — _write_vuln_queue, then _write_vuln_details,
    then _reconcile_unenriched_severity — and asserts the severity is
    corrected WITHOUT a second call to _write_vuln_queue.
    """
    slug = "microsoft-dot_net-cve-2026-9999"
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _detail_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        # No _seed_r7_vuln call — this slug starts with zero enrichment, so
        # the first pass must fall back to the "low" placeholder.
        payload = {"resources": [{"id": slug, "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        # cve_id is the extracted "CVE-2026-9999" (regex over the slug), NOT
        # the slug itself — the slug is Rapid7's r7_vuln_id / r7_vulnerabilities
        # key, which is what enrichment is keyed by.
        cve_id = "CVE-2026-9999"
        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id=cve_id).one()
            assert row.severity == "low", "sanity: unenriched CVE lands on the placeholder"
            assert len(agent._unenriched_vuln_rows) == 1
            got_resource_id, got_cve_id, got_slug, _first_seen = agent._unenriched_vuln_rows[0]
            assert (got_resource_id, got_cve_id, got_slug) == (row.resource_id, cve_id, slug)
            low_due = _utc_naive(row.sla_due)
            anchor = low_due - timedelta(days=90)  # "low"'s SLA window

        # Enrichment phase (MR7) runs later in the SAME run and resolves the
        # slug to a real severity — mirrors _detail_writers' real ordering.
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool") as msol,
        ):
            mdetail.invoke.return_value = _vuln_detail(slug, severity="Critical")
            msols.invoke.return_value = []
            msol.invoke.return_value = None
            agent._write_vuln_details()

        with Session(engine) as s:
            # Enrichment landed in r7_vulnerabilities...
            assert s.query(R7Vulnerability).filter_by(r7_vuln_id=slug).one().severity == "Critical"
            # ...but, pre-fix, vuln_queue would still say "low" here.
            still_placeholder = s.query(VulnQueueItem).filter_by(cve_id=cve_id).one()
            assert still_placeholder.severity == "low", (
                "sanity: enrichment alone (phase 3) must not itself touch vuln_queue"
            )

        # The fix: phase 4 reconciles same-run.
        agent._reconcile_unenriched_severity()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id=cve_id).one()
            assert row.severity == "critical", (
                "severity must resolve to the real enriched band within the SAME "
                "run once enrichment lands, not require the host to be "
                "re-upserted on a later run (GitLab #173)"
            )
            # Confirms the tightening path (_upsert_vuln_row's existing-row
            # branch) actually ran: the deadline moved EARLIER (never later)
            # and recomputed from the original first-seen anchor with
            # critical's 3-day window, matching
            # test_vuln_queue_severity_upgrade_tightens_sla_never_extends'
            # same assertion style for the ordinary (two-run) upgrade path.
            new_due = _utc_naive(row.sla_due)
            assert new_due < low_due, "an upgrade must tighten the deadline, never extend it"
            assert abs((new_due - (anchor + timedelta(days=3))).total_seconds()) < 1


def test_vuln_queue_reconcile_skips_slugs_still_unenriched(session_patcher):
    """A slug that _write_vuln_details did NOT manage to enrich this run
    (e.g. beyond the separate rapid7_vuln_detail_cap) must be left alone by
    the reconciliation pass — it stays "low" until a future run, it must not
    be forced to some wrong value or raise."""
    slug = "microsoft-dot_net-cve-2026-9998"
    cve_id = "CVE-2026-9998"
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {"resources": [{"id": slug, "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        # No enrichment ever lands (simulates falling outside the enrichment
        # cap) — call the reconciliation pass directly, skipping phase 3.
        agent._reconcile_unenriched_severity()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id=cve_id).one()
            assert row.severity == "low"


def test_vuln_queue_reconcile_noop_when_nothing_unenriched(session_patcher):
    """When every CVE this run was already enriched at write time (the
    common case once the fleet has been scanned a few times),
    ``_unenriched_vuln_rows`` is empty and reconciliation is a cheap no-op —
    no extra session/queries."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_r7_vuln(engine, "CVE-2026-9997", severity="High")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {"resources": [{"id": "CVE-2026-9997", "status": "vulnerable"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        assert agent._unenriched_vuln_rows == []
        agent._reconcile_unenriched_severity()  # must not raise

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id="CVE-2026-9997").one()
            assert row.severity == "high"


def test_parse_since_handles_rapid7_formats():
    assert VulnAgent._parse_since(None) is None
    assert VulnAgent._parse_since("") is None
    assert VulnAgent._parse_since("not-a-date") is None
    parsed = VulnAgent._parse_since("2026-01-04T15:44:57.011Z")
    assert parsed == datetime(2026, 1, 4, 15, 44, 57, 11000, tzinfo=UTC)


# ---------------------------------------------------------------------------
# CVE extraction (the String(32) truncation fix)
# ---------------------------------------------------------------------------


def test_extract_cve_from_slug_single():
    """A Rapid7 asset-vuln slug id yields the canonical uppercase CVE id."""
    v = {"id": "microsoft-dot_net_framework-cve-2026-23666", "severity": "Critical"}
    assert VulnAgent._extract_cves(v) == ["CVE-2026-23666"]


def test_extract_cve_prefers_explicit_cves_array():
    """An explicit ``cves`` array (strings or dicts) is preferred over the slug."""
    assert VulnAgent._extract_cves({"id": "slug", "cves": ["CVE-2025-1000", "cve-2025-1001"]}) == [
        "CVE-2025-1000",
        "CVE-2025-1001",
    ]
    assert VulnAgent._extract_cves({"cves": [{"id": "CVE-2025-2000"}]}) == ["CVE-2025-2000"]


def test_extract_cve_multi_from_slug_dedupes():
    """Multiple CVEs in one slug are all extracted, de-duped, order preserved."""
    v = {"id": "vendor-bug-cve-2024-1111-and-cve-2024-2222-also-cve-2024-1111"}
    assert VulnAgent._extract_cves(v) == ["CVE-2024-1111", "CVE-2024-2222"]


def test_extract_cve_none_for_non_cve_check():
    """A non-CVE Rapid7 check (no extractable CVE) yields an empty list → skipped."""
    assert VulnAgent._extract_cves({"id": "ssl-self-signed-certificate", "title": "Weak TLS"}) == []


def test_vuln_queue_stores_extracted_cve_not_slug(session_patcher):
    """The slug id is reduced to a <=32-char CVE id before insert (no truncation)."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        slug = "microsoft-dot_net_framework-cve-2026-23666"  # 42 chars > String(32)
        assert len(slug) > 32
        _seed_r7_vuln(engine, slug, severity="Critical")
        payload = {"resources": [{"id": slug, "severity": "Critical"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).one()
            assert row.cve_id == "CVE-2026-23666"
            assert len(row.cve_id) <= 32  # fits the column — no StringDataRightTruncation
            assert row.severity == "critical"


def test_vuln_queue_multi_cve_fans_out_one_row_each(session_patcher):
    """A finding with multiple CVEs creates one row per (resource_id, cve)."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        slug = "patch-rollup-cve-2026-1001-cve-2026-1002-cve-2026-1003"
        _seed_r7_vuln(engine, slug, severity="High")
        payload = {"resources": [{"id": slug, "severity": "High"}]}
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            cves = {r.cve_id for r in s.query(VulnQueueItem).all()}
            assert cves == {"CVE-2026-1001", "CVE-2026-1002", "CVE-2026-1003"}
            # all share the asset + severity
            assert all(r.severity == "high" for r in s.query(VulnQueueItem).all())


def test_vuln_queue_skips_and_counts_non_cve_findings(session_patcher, caplog):
    """Non-CVE checks are skipped (no row), counted, and logged — never truncated."""
    import logging

    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {
            "resources": [
                {"id": "CVE-2026-5050", "severity": "High"},  # kept
                {"id": "ssl-self-signed-certificate", "severity": "Low"},  # skipped
                {"id": "tlsv1_0-enabled", "severity": "Medium"},  # skipped
            ]
        }
        with caplog.at_level(logging.INFO, logger=MODULE):
            with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
                mock_tool.invoke.return_value = payload
                agent._write_vuln_queue()

        with Session(engine) as s:
            rows = s.query(VulnQueueItem).all()
            assert {r.cve_id for r in rows} == {"CVE-2026-5050"}
        assert any(
            "upserted 1 CVE rows" in r.message and "skipped 2 non-CVE" in r.message
            for r in caplog.records
        )


def test_vuln_queue_dedupes_same_cve_across_findings(session_patcher):
    """The same CVE appearing in two findings on one asset upserts a single row."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {
            "resources": [
                {"id": "rollup-a-cve-2026-7777", "severity": "High"},
                {"id": "rollup-b-cve-2026-7777", "severity": "Critical"},  # same CVE, dupe
            ]
        }
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            agent._write_vuln_queue()

        with Session(engine) as s:
            rows = s.query(VulnQueueItem).all()
            assert len(rows) == 1
            assert rows[0].cve_id == "CVE-2026-7777"


def test_vuln_queue_bad_row_does_not_abort_batch(session_patcher):
    """One bad row is guarded out (SAVEPOINT); the good rows still commit."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]
        payload = {
            "resources": [
                {"id": "CVE-2026-2001", "severity": "High"},
                {"id": "CVE-2026-2002", "severity": "High"},
                {"id": "CVE-2026-2003", "severity": "High"},
            ]
        }
        real_upsert = agent._upsert_vuln_row

        def _flaky(session, resource_id, cve_id, severity, first_seen, now, enriched=True):
            if cve_id == "CVE-2026-2002":
                raise RuntimeError("simulated bad row")
            return real_upsert(
                session, resource_id, cve_id, severity, first_seen, now, enriched=enriched
            )

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = payload
            with patch.object(agent, "_upsert_vuln_row", side_effect=_flaky):
                agent._write_vuln_queue()

        with Session(engine) as s:
            cves = {r.cve_id for r in s.query(VulnQueueItem).all()}
            # The good rows committed; the failing one was guarded out.
            assert cves == {"CVE-2026-2001", "CVE-2026-2003"}


def test_vuln_queue_failed_row_write_does_not_auto_close_existing_open_row(session_patcher):
    """Regression test: a CVE whose write merely FAILS this run (e.g. a
    transient DB error, an over-length value) must not be indistinguishable
    from a genuinely-remediated CVE. Before the fix, a failed write did
    ``seen_keys.discard(key)``, which made the FE-9 stale-row reconciliation
    (scoped to `seen_keys`) treat it as "no longer reported by Rapid7" and
    auto-close it as CLOSED_AUTO — hiding a still-open vulnerability."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _feeder_agent()
        agent._last_assets = [_asset(1, "srv-01")]

        # First run: CVE-2026-3001 writes successfully and lands as "open".
        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {
                "resources": [{"id": "CVE-2026-3001", "severity": "High"}]
            }
            agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id="CVE-2026-3001").one()
            assert row.status == "open"

        # Second run: Rapid7 STILL reports CVE-2026-3001 (it is not
        # remediated), but the write for it fails this run (simulated bad
        # row / transient error).
        real_upsert = agent._upsert_vuln_row

        def _flaky(session, resource_id, cve_id, severity, first_seen, now, enriched=True):
            if cve_id == "CVE-2026-3001":
                raise RuntimeError("simulated transient write failure")
            return real_upsert(
                session, resource_id, cve_id, severity, first_seen, now, enriched=enriched
            )

        with patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mock_tool:
            mock_tool.invoke.return_value = {
                "resources": [{"id": "CVE-2026-3001", "severity": "High"}]
            }
            with patch.object(agent, "_upsert_vuln_row", side_effect=_flaky):
                agent._write_vuln_queue()

        with Session(engine) as s:
            row = s.query(VulnQueueItem).filter_by(cve_id="CVE-2026-3001").one()
            assert row.status == "open", (
                "a CVE that merely failed to WRITE this run (still reported by "
                "Rapid7) must never be auto-closed as CLOSED_AUTO — that hides a "
                "real, still-open vulnerability"
            )


def test_write_details_surfaces_vuln_failure(sqlite_engine):
    """A failure in the feeder must flip the CollectionRun to failed."""
    from contextlib import contextmanager

    from infra_brain.agents.base import CollectionResult

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
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _feeder_agent()
    result = CollectionResult(
        run_id=run_id, domain="vuln", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated feeder failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated feeder failure" in e for e in result.errors)
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "failed"


# ---------------------------------------------------------------------------
# r7_assets / r7_sites / r7_tags relational writer (MR6)
# ---------------------------------------------------------------------------


def _rich_asset(
    asset_id=1, name="srv-01", ip="10.0.0.1", with_fp=True, risk=500.0, atype="virtual"
):
    """A real fleet asset list-item: os STRING + osFingerprint dict + bands."""
    asset = {
        "id": asset_id,
        "hostName": name,
        "ip": ip,
        "mac": "00:11:22:33:44:55",
        "os": "Microsoft Windows Server 2019 Datacenter Edition 1809",
        "riskScore": risk,
        "rawRiskScore": risk + 1.0,
        "type": atype,
        "assessedForVulnerabilities": True,
        "vulnerabilities": {
            "critical": 4,
            "severe": 9,
            "moderate": 2,
            "total": 15,
            "exploits": 3,
            "malwareKits": 1,
        },
        "software": [{"product": "p", "vendor": "v", "version": "1", "id": 7}],
        "users": [{"name": "admin"}],
        "configurations": [{"name": "c"}],
        "addresses": [{"ip": ip, "mac": "00:11:22:33:44:55"}],
        "ids": [{"id": "abc", "source": "epolicy"}],
        "hostNames": [{"name": name, "source": "dns"}],
    }
    if with_fp:
        asset["osFingerprint"] = {
            "family": "Windows",
            "product": "Windows Server 2019 Datacenter Edition",
            "version": "1809",
            "vendor": "Microsoft",
            "architecture": "x86_64",
        }
    else:
        asset["osFingerprint"] = None
    return asset


def _noise_asset(asset_id=99, ip="8.8.8.8"):
    """External/internet IP: no fingerprint, risk 0, type null — network noise."""
    return {
        "id": asset_id,
        "ip": ip,
        "hostName": None,
        "osFingerprint": None,
        "riskScore": 0,
        "type": None,
        "vulnerabilities": {"total": 0},
    }


def _rel_agent():
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def test_r7_assets_upsert_with_os_and_fingerprint_and_bands(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")  # the canonical r7_asset Resource
        agent = _rel_agent()
        agent._last_assets = [_rich_asset(1, "srv-01")]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            agent._write_r7_relational()

        with Session(engine) as s:
            row = s.query(R7Asset).one()
            assert row.r7_asset_id == 1
            # OS bug fix: os is the STRING, os_* from osFingerprint.
            assert row.os == "Microsoft Windows Server 2019 Datacenter Edition 1809"
            assert row.os_family == "Windows"
            assert row.os_product == "Windows Server 2019 Datacenter Edition"
            assert row.os_version == "1809"
            assert row.os_vendor == "Microsoft"
            assert row.os_architecture == "x86_64"
            assert row.asset_type == "virtual"
            assert row.risk_score == 500.0
            assert row.raw_risk_score == 501.0
            assert row.assessed_for_vulnerabilities is True
            # The six Rapid7 bands (critical/severe/moderate — NO high).
            assert row.vuln_critical == 4
            assert row.vuln_severe == 9
            assert row.vuln_moderate == 2
            assert row.vuln_total == 15
            assert row.vuln_exploits == 3
            assert row.vuln_malware_kits == 1
            # The big nested lists land in details JSONB.
            assert set(row.details) == {
                "software",
                "users",
                "configurations",
                "addresses",
                "ids",
                "hostNames",
            }
            assert row.details["software"][0]["product"] == "p"
            # Linked back to the canonical Resource.
            res = s.query(Resource).filter_by(type="r7_asset", name="srv-01").one()
            assert row.resource_id == res.id


def test_r7_assets_noise_filter_skips_and_counts(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        agent._last_assets = [
            _rich_asset(1, "srv-01"),  # real (has fingerprint)
            _noise_asset(99, "8.8.8.8"),  # noise (no fp, risk 0, type null)
            _noise_asset(100, "1.1.1.1"),  # noise
        ]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            with caplog.at_level(logging.INFO, logger=MODULE):
                agent._write_r7_relational()

        with Session(engine) as s:
            ids = {r.r7_asset_id for r in s.query(R7Asset).all()}
            assert ids == {1}  # only the real asset
        assert any(
            "3 assets total, 1 real, 2 filtered as network-noise" in r.message
            for r in caplog.records
        )


def test_r7_assets_noise_filter_keeps_risk_or_type_only(session_patcher):
    """An asset with no fingerprint but riskScore>0 OR a non-null type is kept."""
    with session_patcher(MODULE) as engine:
        agent = _rel_agent()
        agent._last_assets = [
            _rich_asset(2, "noname-risk", with_fp=False, risk=42.0, atype=None),  # risk>0
            {
                "id": 3,
                "osFingerprint": None,
                "riskScore": 0,
                "type": "physical",
                "vulnerabilities": {},
            },  # type only
            _noise_asset(4),  # dropped
        ]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            agent._write_r7_relational()
        with Session(engine) as s:
            assert {r.r7_asset_id for r in s.query(R7Asset).all()} == {2, 3}


def test_r7_assets_idempotent_upsert(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        agent._last_assets = [_rich_asset(1, "srv-01", risk=100.0)]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            agent._write_r7_relational()
            # second pass — same asset, refreshed risk
            agent._last_assets = [_rich_asset(1, "srv-01", risk=250.0)]
            agent._write_r7_relational()
        with Session(engine) as s:
            rows = s.query(R7Asset).all()
            assert len(rows) == 1  # no duplicate
            assert rows[0].risk_score == 250.0  # refreshed


def test_r7_assets_bad_asset_guarded_out_siblings_survive(session_patcher):
    """DL-C-5/DL-C-6: one asset whose write raises is guarded out by its own
    SAVEPOINT (session.begin_nested()); the other assets in the same batch still
    commit — previously the whole batch shared one transaction with no SAVEPOINT,
    so a single bad asset rolled back every other asset's r7_assets row too."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_asset(engine, 2, "srv-02")
        agent = _rel_agent()
        agent._last_assets = [
            _rich_asset(1, "srv-01"),
            _rich_asset(2, "srv-02"),
        ]
        real_children = agent._write_asset_children

        def _flaky_children(session, r7_asset, asset):
            if r7_asset.r7_asset_id == 2:
                raise RuntimeError("simulated bad asset row")
            return real_children(session, r7_asset, asset)

        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
            patch.object(agent, "_write_asset_children", side_effect=_flaky_children),
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            written = agent._write_r7_relational()

        with Session(engine) as s:
            ids = {r.r7_asset_id for r in s.query(R7Asset).all()}
            # asset 2's write (including its own R7Asset upsert) was rolled back by
            # its SAVEPOINT; asset 1 still committed.
            assert ids == {1}
        # No-silent-partial-success: the return value (fed into
        # CollectionResult.detail_rows_written by _write_details) reflects the
        # real count, not the full batch size.
        assert written == 1


def test_r7_assets_all_written_returns_full_count(session_patcher):
    """The happy path returns len(real) so detail_rows_written reflects reality."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        _seed_asset(engine, 2, "srv-02")
        agent = _rel_agent()
        agent._last_assets = [_rich_asset(1, "srv-01"), _rich_asset(2, "srv-02")]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            written = agent._write_r7_relational()
        assert written == 2


def test_r7_sites_and_tags_upsert(session_patcher):
    with session_patcher(MODULE) as engine:
        agent = _rel_agent()
        agent._last_assets = []
        sites = [
            {
                "id": 1,
                "name": "Corporate",
                "assets": 240,
                "riskScore": 12345.6,
                "importance": "high",
                "type": "static",
                "lastScanTime": "2026-06-20T03:00:00Z",
                "vulnerabilities": {"total": 99, "critical": 5},
            },
            {"id": 2, "name": "DMZ", "assets": 10},
        ]
        tags = [
            {"id": 1, "name": "owner:infra", "type": "owner", "color": "blue", "source": "manual"},
            {"id": 2, "name": "criticality:high", "type": "criticality"},
        ]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
        ):
            msites.invoke.return_value = sites
            mtags.invoke.return_value = tags
            agent._write_r7_relational()
            agent._write_r7_relational()  # idempotency

        with Session(engine) as s:
            site_rows = {r.r7_site_id: r for r in s.query(R7Site).all()}
            assert set(site_rows) == {1, 2}  # idempotent
            corp = site_rows[1]
            assert corp.name == "Corporate"
            assert corp.asset_count == 240
            assert corp.risk_score == 12345.6
            assert corp.importance == "high"
            assert corp.site_type == "static"
            assert corp.last_scan_time.date().isoformat() == "2026-06-20"
            assert corp.details["vulnerabilities"]["critical"] == 5

            tag_rows = {r.r7_tag_id: r for r in s.query(R7Tag).all()}
            assert set(tag_rows) == {1, 2}
            assert tag_rows[1].tag_type == "owner"
            assert tag_rows[1].color == "blue"
            assert tag_rows[1].source == "manual"


def test_vuln_queue_feeder_still_works_alongside_relational(session_patcher):
    """MR3 vuln_queue feeder is unaffected by the new relational writer."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        agent.settings.rapid7_vuln_asset_cap = 750
        agent._last_assets = [_rich_asset(1, "srv-01")]
        with (
            patch(f"{MODULE}.rapid7_sites_tool") as msites,
            patch(f"{MODULE}.rapid7_tags_tool") as mtags,
            patch(f"{MODULE}.rapid7_asset_vulnerabilities_tool") as mvulns,
        ):
            msites.invoke.return_value = []
            mtags.invoke.return_value = []
            mvulns.invoke.return_value = {
                "resources": [{"id": "CVE-2026-4242", "severity": "Critical"}]
            }
            agent._write_r7_relational()
            agent._write_vuln_queue()

        with Session(engine) as s:
            assert s.query(R7Asset).count() == 1
            assert {r.cve_id for r in s.query(VulnQueueItem).all()} == {"CVE-2026-4242"}


# ---------------------------------------------------------------------------
# MR7: r7_assets child tables (software/config/users/addresses) from asset data
# ---------------------------------------------------------------------------


def _child_asset(asset_id=1, name="srv-01"):
    """A real asset with the four nested child lists populated."""
    a = _rich_asset(asset_id, name)
    a["software"] = [
        {"product": "OpenSSL", "vendor": "OpenSSL", "version": "1.1.1k", "type": "library"},
        {"product": "nginx", "vendor": "F5", "version": "1.20.0", "description": "web server"},
    ]
    a["configurations"] = [
        {"name": "cpuinfo.0.vendor", "value": "GenuineIntel"},
        {"name": "memory.total", "value": "16384 MB"},
    ]
    a["users"] = [
        {"id": 1, "name": "administrator", "fullName": "Local Admin"},
        {"id": 2, "name": "svc_backup"},
    ]
    a["addresses"] = [
        {"ip": "10.0.0.1", "mac": "00:11:22:33:44:55"},
        {"ip": "fe80::1", "mac": None},
    ]
    return a


def _relational_with(agent, engine):
    """Run _write_r7_relational with sites/tags mocked empty."""
    with (
        patch(f"{MODULE}.rapid7_sites_tool") as msites,
        patch(f"{MODULE}.rapid7_tags_tool") as mtags,
    ):
        msites.invoke.return_value = []
        mtags.invoke.return_value = []
        agent._write_r7_relational()


def test_r7_asset_children_populated_from_asset_data(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        agent._last_assets = [_child_asset(1, "srv-01")]
        _relational_with(agent, engine)

        with Session(engine) as s:
            asset = s.query(R7Asset).one()
            sw = {r.product: r for r in s.query(R7Software).all()}
            assert set(sw) == {"OpenSSL", "nginx"}
            assert sw["OpenSSL"].vendor == "OpenSSL"
            assert sw["OpenSSL"].version == "1.1.1k"
            assert sw["OpenSSL"].software_type == "library"
            # type falls back to description when no explicit type.
            assert sw["nginx"].software_type == "web server"
            # child linked to the r7_assets row + denormalized upstream id.
            assert sw["OpenSSL"].asset_id == asset.id
            assert sw["OpenSSL"].r7_asset_id == 1

            cfg = {r.name: r.value for r in s.query(R7AssetConfig).all()}
            assert cfg == {"cpuinfo.0.vendor": "GenuineIntel", "memory.total": "16384 MB"}

            users = {r.username: r for r in s.query(R7AssetUser).all()}
            assert set(users) == {"administrator", "svc_backup"}
            assert users["administrator"].full_name == "Local Admin"
            assert users["svc_backup"].full_name is None

            addrs = {r.ip: r for r in s.query(R7AssetAddress).all()}
            assert set(addrs) == {"10.0.0.1", "fe80::1"}
            assert addrs["10.0.0.1"].mac == "00:11:22:33:44:55"
            assert addrs["fe80::1"].mac is None


def test_r7_asset_children_delete_reinsert_idempotent(session_patcher):
    """Re-running replaces the child set (delete-then-reinsert) — no duplicates,
    and removed items disappear."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        agent._last_assets = [_child_asset(1, "srv-01")]
        _relational_with(agent, engine)

        # Second scan: one fewer software, one changed config value.
        second = _child_asset(1, "srv-01")
        second["software"] = [
            {"product": "OpenSSL", "vendor": "OpenSSL", "version": "3.0.0", "type": "library"}
        ]
        second["configurations"] = [{"name": "memory.total", "value": "32768 MB"}]
        agent._last_assets = [second]
        _relational_with(agent, engine)

        with Session(engine) as s:
            sw = s.query(R7Software).all()
            assert len(sw) == 1  # nginx gone, no dupes
            assert sw[0].version == "3.0.0"  # refreshed
            cfg = {r.name: r.value for r in s.query(R7AssetConfig).all()}
            assert cfg == {"memory.total": "32768 MB"}  # old key dropped


def test_r7_asset_children_dedupe_within_scan(session_patcher):
    """Duplicate natkeys within one scan don't violate the unique constraint."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        a = _child_asset(1, "srv-01")
        a["software"] = [
            {"product": "OpenSSL", "version": "1.1.1k"},
            {"product": "OpenSSL", "version": "1.1.1k"},  # exact dup
        ]
        a["addresses"] = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.1"}]  # dup ip
        agent._last_assets = [a]
        _relational_with(agent, engine)
        with Session(engine) as s:
            assert s.query(R7Software).count() == 1
            assert s.query(R7AssetAddress).count() == 1


def test_r7_asset_sites_persisted_from_asset_data(session_patcher):
    """TRK-104: the asset list-item ``sites`` array is persisted into the
    r7_asset_sites bridge (many-to-many asset↔site membership)."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        a = _child_asset(1, "srv-01")
        a["sites"] = [10, 20, 20]  # dup id within scan collapses to one row
        agent._last_assets = [a]
        _relational_with(agent, engine)
        with Session(engine) as s:
            links = {r.r7_site_id for r in s.query(R7AssetSite).filter_by(r7_asset_id=1).all()}
            assert links == {10, 20}


def test_r7_asset_sites_delete_reinsert_idempotent(session_patcher):
    """Re-running replaces the site set (delete-then-reinsert) — no dupes; a
    dropped site disappears."""
    with session_patcher(MODULE) as engine:
        _seed_asset(engine, 1, "srv-01")
        agent = _rel_agent()
        a = _child_asset(1, "srv-01")
        a["sites"] = [10, 20]
        agent._last_assets = [a]
        _relational_with(agent, engine)
        # Second scan: site 20 removed.
        second = _child_asset(1, "srv-01")
        second["sites"] = [10]
        agent._last_assets = [second]
        _relational_with(agent, engine)
        with Session(engine) as s:
            links = {r.r7_site_id for r in s.query(R7AssetSite).all()}
            assert links == {10}


# ---------------------------------------------------------------------------
# MR7: vuln detail + solutions enrichment (bounded + cached)
# ---------------------------------------------------------------------------


def _detail_agent(detail_cap=600, asset_cap=750):
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_detail_cap = detail_cap
    agent.settings.rapid7_vuln_asset_cap = asset_cap
    agent.settings.default_zone = "corpor"
    agent.callbacks = []
    return agent


def _vuln_detail(slug, severity="Critical", pci_fail=True):
    return {
        "id": slug,
        "title": f"Title for {slug}",
        "severity": severity,
        "riskScore": 850.5,
        "cvss": {
            "v3": {"score": 9.8, "vector": "CVSS:3.1/AV:N/AC:L"},
            "v2": {"score": 7.5},
        },
        "exploits": 2,
        "malwareKits": 1,
        "denialOfService": False,
        "published": "2023-10-10T00:00:00Z",
        "categories": ["Microsoft", "ASP.NET"],
        "cves": ["CVE-2023-36038"],
        "pci": {"fail": pci_fail, "status": "fail" if pci_fail else "pass"},
    }


def _solution(sol_id):
    return {
        "id": sol_id,
        "summary": {"text": f"summary {sol_id}", "html": "<p>x</p>"},
        "steps": {"text": f"do the fix {sol_id}", "html": "<p>fix</p>"},
        "type": "rollup-patch",
        "estimate": "PT10M",
    }


def test_vuln_details_populated_for_distinct_slugs(session_patcher):
    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {"microsoft-asp_net_core-cve-2023-36038": "critical"}
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool") as msol,
        ):
            mdetail.invoke.return_value = _vuln_detail("microsoft-asp_net_core-cve-2023-36038")
            msols.invoke.return_value = ["sol-aspnet-upgrade"]
            msol.invoke.return_value = _solution("sol-aspnet-upgrade")
            agent._write_vuln_details()

        with Session(engine) as s:
            v = s.query(R7Vulnerability).one()
            assert v.r7_vuln_id == "microsoft-asp_net_core-cve-2023-36038"
            assert v.title == "Title for microsoft-asp_net_core-cve-2023-36038"
            assert v.severity == "Critical"
            assert v.cvss_v3_score == 9.8
            assert v.cvss_v3_vector == "CVSS:3.1/AV:N/AC:L"
            assert v.cvss_v2_score == 7.5
            assert v.risk_score == 850.5
            assert v.exploits == 2
            assert v.malware_kits == 1
            assert v.denial_of_service is False
            assert v.pci_status == "fail"
            assert v.pci_fail is True
            assert v.published.year == 2023
            assert v.categories == ["Microsoft", "ASP.NET"]
            assert v.cves == ["CVE-2023-36038"]
            assert v.fix_available is True  # has a linked solution

            sol = s.query(R7Solution).one()
            assert sol.r7_solution_id == "sol-aspnet-upgrade"
            assert sol.summary == "summary sol-aspnet-upgrade"  # text preferred over html
            assert sol.steps == "do the fix sol-aspnet-upgrade"
            assert sol.solution_type == "rollup-patch"
            assert sol.estimate == "PT10M"

            link = s.query(R7VulnSolution).one()
            assert link.r7_vuln_id == "microsoft-asp_net_core-cve-2023-36038"
            assert link.r7_solution_id == "sol-aspnet-upgrade"

            # CVE bridge populated from the vuln's cves array.
            cve_link = s.query(R7VulnCve).one()
            assert cve_link.r7_vuln_id == "microsoft-asp_net_core-cve-2023-36038"
            assert cve_link.cve_id == "CVE-2023-36038"


def test_vuln_details_reenrichment_preserves_fix_available_on_solutions_outage(
    session_patcher,
):
    """L-3: a transient rapid7_vuln_solutions_tool failure during
    re-enrichment must not erase a previously-known fix_available=True.

    ``_vuln_detail_row`` unconditionally set ``fix_available: None`` on every
    (re-)upsert of the vuln-detail row, only restoring True afterwards if the
    solutions fetch happened to succeed. A transient solutions-endpoint
    outage on a re-enrichment pass therefore hard-reset a known
    fix_available=True to None with no error recorded.
    """
    slug = "microsoft-asp_net_core-cve-2023-36038"
    with session_patcher(MODULE) as engine:
        # First pass: solutions fetch succeeds -> fix_available=True established.
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {slug: "critical"}
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool") as msol,
        ):
            mdetail.invoke.return_value = _vuln_detail(slug)
            msols.invoke.return_value = ["sol-aspnet-upgrade"]
            msol.invoke.return_value = _solution("sol-aspnet-upgrade")
            agent._write_vuln_details()

        with Session(engine) as s:
            v = s.query(R7Vulnerability).filter_by(r7_vuln_id=slug).one()
            assert v.fix_available is True

        # Second pass (re-enrichment, fresh agent instance): the vuln detail
        # fetch still succeeds, but the SOLUTIONS endpoint transiently fails.
        agent2 = _detail_agent()
        agent2._distinct_vuln_slugs = {slug: "critical"}
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
        ):
            mdetail.invoke.return_value = _vuln_detail(slug)
            msols.invoke.side_effect = RuntimeError("simulated Rapid7 solutions outage")
            agent2._write_vuln_details()

        with Session(engine) as s:
            v = s.query(R7Vulnerability).filter_by(r7_vuln_id=slug).one()
            assert v.fix_available is True, (
                "a transient solutions-fetch failure must not erase a known fix_available=True"
            )


def test_vuln_cve_bridge_multi_cve_fans_out_one_row_each(session_patcher):
    """A vuln whose cves array carries multiple CVEs yields one bridge row each."""
    slug = "microsoft-multi-cve-vuln"
    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {slug: "critical"}
        detail = _vuln_detail(slug)
        detail["cves"] = ["CVE-2024-0001", "cve-2024-0002", "CVE-2024-0003"]
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
        ):
            mdetail.invoke.return_value = detail
            msols.invoke.return_value = []
            agent._write_vuln_details()

        with Session(engine) as s:
            rows = s.query(R7VulnCve).filter_by(r7_vuln_id=slug).all()
            cves = sorted(r.cve_id for r in rows)
            # All uppercased, one row per distinct CVE.
            assert cves == ["CVE-2024-0001", "CVE-2024-0002", "CVE-2024-0003"]


def test_vuln_cve_bridge_idempotent_and_drops_stale(session_patcher):
    """Re-running upserts the same rows (delete-then-reinsert), and a CVE that
    drops from a re-fetched payload also drops from the bridge."""
    slug = "microsoft-stale-cve-vuln"
    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {slug: "critical"}

        first = _vuln_detail(slug)
        first["cves"] = ["CVE-2024-1111", "CVE-2024-2222"]
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
        ):
            mdetail.invoke.return_value = first
            msols.invoke.return_value = []
            agent._write_vuln_details()
            with Session(engine) as s:
                assert s.query(R7VulnCve).filter_by(r7_vuln_id=slug).count() == 2

            # Re-fetch: one CVE dropped, one added.
            second = _vuln_detail(slug)
            second["cves"] = ["CVE-2024-1111", "CVE-2024-3333"]
            mdetail.invoke.return_value = second
            agent._write_vuln_details()

        with Session(engine) as s:
            cves = sorted(r.cve_id for r in s.query(R7VulnCve).filter_by(r7_vuln_id=slug).all())
            # No duplicate of the unchanged CVE; stale 2222 gone; new 3333 present.
            assert cves == ["CVE-2024-1111", "CVE-2024-3333"]


def test_clean_walk_vuln_queue_to_r7_vulnerability_via_bridge(session_patcher):
    """The full join resolves: vuln_queue.cve_id -> r7_vuln_cves.cve_id ->
    r7_vuln_id -> r7_vulnerabilities (CVSS) -> r7_vuln_solutions -> r7_solutions."""
    slug = "microsoft-asp_net_core-cve-2023-36038"
    cve = "CVE-2023-36038"
    with session_patcher(MODULE) as engine:
        # 1. Enrich: populate r7_vulnerabilities + r7_vuln_cves + solutions.
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {slug: "critical"}
        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool") as msol,
        ):
            mdetail.invoke.return_value = _vuln_detail(slug)
            msols.invoke.return_value = ["sol-aspnet-upgrade"]
            msol.invoke.return_value = _solution("sol-aspnet-upgrade")
            agent._write_vuln_details()

        # 2. Seed a vuln_queue row keyed by the canonical CVE.
        resource_id = _seed_asset(engine, 1, "srv-01")
        with Session(engine) as s:
            s.add(
                VulnQueueItem(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    cve_id=cve,
                    severity="critical",
                )
            )
            s.commit()

        # 3. Walk the clean join the production query uses.
        with Session(engine) as s:
            row = (
                s.query(
                    VulnQueueItem.cve_id,
                    R7Vulnerability.cvss_v3_score,
                    R7Solution.steps,
                )
                .join(R7VulnCve, R7VulnCve.cve_id == VulnQueueItem.cve_id)
                .join(R7Vulnerability, R7Vulnerability.r7_vuln_id == R7VulnCve.r7_vuln_id)
                .join(R7VulnSolution, R7VulnSolution.r7_vuln_id == R7Vulnerability.r7_vuln_id)
                .join(R7Solution, R7Solution.r7_solution_id == R7VulnSolution.r7_solution_id)
                .one()
            )
            assert row.cve_id == cve
            assert row.cvss_v3_score == 9.8
            assert row.steps == "do the fix sol-aspnet-upgrade"


def test_vuln_details_pci_fail_from_status_only(session_patcher):
    """pci_fail derives from pci.status when no explicit boolean ``fail`` key."""
    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {"slug-pass": "low", "slug-fail": "high"}
        detail_pass = _vuln_detail("slug-pass")
        detail_pass["pci"] = {"status": "pass"}  # no ``fail`` boolean
        detail_fail = _vuln_detail("slug-fail")
        detail_fail["pci"] = {"status": "fail"}

        def _detail(payload, config=None):
            return {"slug-pass": detail_pass, "slug-fail": detail_fail}[payload["vuln_id"]]

        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
        ):
            mdetail.invoke.side_effect = _detail
            msols.invoke.return_value = []
            agent._write_vuln_details()

        with Session(engine) as s:
            rows = {r.r7_vuln_id: r for r in s.query(R7Vulnerability).all()}
            assert rows["slug-pass"].pci_fail is False
            assert rows["slug-fail"].pci_fail is True


def test_vuln_details_respects_cap_and_severity_priority(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        agent = _detail_agent(detail_cap=2)
        # 4 distinct slugs of varying severity; cap=2 keeps the two most severe.
        agent._distinct_vuln_slugs = {
            "slug-crit": "critical",
            "slug-sev": "severe",
            "slug-mod": "moderate",
            "slug-low": "low",
        }
        fetched = []

        def _detail(payload, config=None):
            fetched.append(payload["vuln_id"])
            return _vuln_detail(payload["vuln_id"])

        with caplog.at_level(logging.INFO, logger=BRIDGE_MODULE):
            with (
                patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
                patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
                patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
            ):
                mdetail.invoke.side_effect = _detail
                msols.invoke.return_value = []
                agent._write_vuln_details()

        # Only the top-2 by severity were enriched.
        assert set(fetched) == {"slug-crit", "slug-sev"}
        with Session(engine) as s:
            assert {r.r7_vuln_id for r in s.query(R7Vulnerability).all()} == {
                "slug-crit",
                "slug-sev",
            }
        assert any(
            "4 distinct vulns found" in r.message
            and "cap=2" in r.message
            and "2 skipped" in r.message
            for r in caplog.records
        )


def test_vuln_details_solution_cache_fetches_once(session_patcher):
    """A solution shared by two vulns is fetched exactly once (run-level cache)."""
    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {"slug-a": "critical", "slug-b": "high"}
        sol_calls = []

        def _detail(payload, config=None):
            return _vuln_detail(payload["vuln_id"])

        def _sol(payload, config=None):
            sol_calls.append(payload["solution_id"])
            return _solution(payload["solution_id"])

        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool") as msol,
        ):
            mdetail.invoke.side_effect = _detail
            msols.invoke.return_value = ["shared-sol"]  # both vulns link the same solution
            msol.invoke.side_effect = _sol
            agent._write_vuln_details()

        # Solution fetched once despite two vulns referencing it.
        assert sol_calls == ["shared-sol"]
        with Session(engine) as s:
            assert s.query(R7Solution).count() == 1
            # Both vulns linked to it.
            assert s.query(R7VulnSolution).count() == 2


def test_vuln_details_bad_vuln_does_not_abort_batch(session_patcher):
    """A simulated failure on one vuln is guarded out; the others still commit."""
    from infra_brain.agents.vuln_cve import VulnCveBridge

    with session_patcher(MODULE) as engine:
        agent = _detail_agent()
        agent._distinct_vuln_slugs = {"good-1": "critical", "bad": "critical", "good-2": "high"}

        def _detail(payload, config=None):
            return _vuln_detail(payload["vuln_id"])

        real_row = VulnCveBridge._vuln_detail_row

        def _flaky_row(self, slug, detail):
            if slug == "bad":
                raise RuntimeError("simulated bad vuln row")
            return real_row(self, slug, detail)

        with (
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_detail_tool") as mdetail,
            patch(f"{BRIDGE_MODULE}.rapid7_vuln_solutions_tool") as msols,
            patch(f"{BRIDGE_MODULE}.rapid7_solution_tool"),
            patch.object(VulnCveBridge, "_vuln_detail_row", _flaky_row),
        ):
            mdetail.invoke.side_effect = _detail
            msols.invoke.return_value = []
            agent._write_vuln_details()

        with Session(engine) as s:
            assert {r.r7_vuln_id for r in s.query(R7Vulnerability).all()} == {"good-1", "good-2"}


def test_vuln_details_write_surfaces_failure(sqlite_engine):
    """A failure in the enrichment phase flips the CollectionRun to failed."""
    from contextlib import contextmanager

    from infra_brain.agents.base import CollectionResult

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
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _detail_agent()
    result = CollectionResult(
        run_id=run_id, domain="vuln", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated enrichment failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated enrichment failure" in e for e in result.errors)
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "failed"


def test_env_parity_rapid7_vuln_detail_cap():
    """The new RAPID7_VULN_DETAIL_CAP setting is present in config + .env.example."""
    import pathlib

    from infra_brain.config import Settings

    assert hasattr(Settings(), "rapid7_vuln_detail_cap")
    env = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
    assert "RAPID7_VULN_DETAIL_CAP" in env.read_text()


# ``test_vulnerable_to_skips_self_loop_when_vuln_has_no_resource`` — DELETED
# (P5). It drove the real ``VulnAgent._write_graph_edges`` against a seeded
# sqlite session to pin Fix 1.5: a VULNERABLE_TO edge whose vulnerability has
# no ``resources`` row must be SKIPPED rather than emitted as a self-loop. P5
# deleted that method with the ``resource_relationships`` store it was the only
# writer into (see its epitaph in agents/vuln.py), so both the guard and the
# self-loop it guarded against are gone — there is no code path left that could
# regress.
#
# Not ported to ``graph_edges``: this edge did not migrate, it went away
# (Rapid7 is not deployed, the collector is retired, zero rows ever). A port
# would test a declaration nobody wrote. Note the invariant itself did not
# depend on this method — ``emit_edges_batch`` drops self-loops centrally
# (F-022), and ``graph_engine`` resolves endpoints from declared NodeSpecs
# rather than fabricating them, so the shape of bug Fix 1.5 addressed is
# structurally unavailable in the new store.
#
# The surviving vuln coverage in this file — asset/detail writes, the
# vuln_queue feeder, severity reconciliation, the asset cap and rotation — is
# untouched and still green.


# ---------------------------------------------------------------------------
# Truncation counters (Phase 4 / OB hygiene): _bounded_assets writes an
# AgentActionLog row when the rapid7_vuln_asset_cap trims the asset list.
# ---------------------------------------------------------------------------


def _cap_asset(idx, risk=100):
    return {
        "id": idx,
        "hostName": f"srv-{idx}",
        "vulnerabilities": {"critical": 1, "total": 1},
        "riskScore": risk,
    }


def test_bounded_assets_over_cap_writes_truncation_row(sqlite_engine):
    """When the asset cap trims the list, one AgentActionLog row is written
    with tool=truncation, verdict=allow, status=ok, and the correct
    dropped_count/cap/entity in args_summary."""
    import json
    from contextlib import contextmanager

    from infra_brain.db.models import AgentActionLog

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_asset_cap = 2
    agent.callbacks = []
    run_id = uuid.uuid4()
    agent._active_run_id = run_id

    assets = [_cap_asset(i) for i in range(5)]  # 5 with-vulns assets, cap=2

    with patch("infra_brain.agents.vuln.get_session", _get_session):
        selected = agent._bounded_assets(assets)

    assert len(selected) == 2

    with Session(sqlite_engine) as s:
        rows = s.query(AgentActionLog).filter_by(tool="truncation").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.verdict == "allow"
        assert row.status == "ok"
        assert row.domain == "vuln"
        assert row.run_id == str(run_id)
        summary = json.loads(row.args_summary)
        assert summary["cap"] == 2
        assert summary["dropped_count"] == 3  # 5 - 2
        assert summary["entity"] == "rapid7_vuln_asset_cap"


def test_bounded_assets_under_cap_writes_no_truncation_row(sqlite_engine):
    """No AgentActionLog truncation row is written when the asset count stays
    under the cap."""
    from contextlib import contextmanager

    from infra_brain.db.models import AgentActionLog

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_asset_cap = 750
    agent.callbacks = []

    assets = [_cap_asset(i) for i in range(3)]  # well under cap

    with patch("infra_brain.agents.vuln.get_session", _get_session):
        selected = agent._bounded_assets(assets)

    assert len(selected) == 3
    with Session(sqlite_engine) as s:
        assert s.query(AgentActionLog).filter_by(tool="truncation").count() == 0


# ---------------------------------------------------------------------------
# Coverage rotation (GitLab #173 / #188 Bug 2): _apply_cap_with_rotation
# reserves a slice of the asset cap for round-robin coverage of the assets
# ranked below the guaranteed top, so no asset is starved of vuln_queue
# refreshes forever just because it never re-enters the top-N by risk.
# ---------------------------------------------------------------------------


def _ranked_assets(n):
    """n assets ranked risk-desc: id 0 is highest risk, id n-1 lowest."""
    return [_cap_asset(i, risk=1000 - i) for i in range(n)]


def test_rotation_noop_when_under_cap():
    """Cap not binding => selection is the input, byte-identical, regardless
    of slots/ordinal (rotation only redistributes scarcity)."""
    ranked = _ranked_assets(5)
    for ordinal in (0, 1, 7):
        out = VulnAgent._apply_cap_with_rotation(ranked, cap=10, slots=3, run_ordinal=ordinal)
        assert out == ranked


def test_rotation_slots_zero_is_pure_top_cap():
    ranked = _ranked_assets(10)
    out = VulnAgent._apply_cap_with_rotation(ranked, cap=4, slots=0, run_ordinal=5)
    assert out == ranked[:4]


def test_rotation_ordinal_zero_matches_old_behavior():
    """ordinal=0 (unit tests / ordinal-read failure) => exactly ranked[:cap]."""
    ranked = _ranked_assets(10)
    out = VulnAgent._apply_cap_with_rotation(ranked, cap=4, slots=2, run_ordinal=0)
    assert out == ranked[:4]


def test_rotation_guaranteed_top_always_selected():
    """The top (cap - slots) by risk are present for every ordinal."""
    ranked = _ranked_assets(20)
    guaranteed_ids = {a["id"] for a in ranked[:6]}  # cap=10, slots=4
    for ordinal in range(15):
        out = VulnAgent._apply_cap_with_rotation(ranked, cap=10, slots=4, run_ordinal=ordinal)
        assert len(out) == 10
        assert guaranteed_ids <= {a["id"] for a in out}


def test_rotation_covers_every_asset_within_ceil_r_over_slots_runs():
    """Across ceil(R/slots) consecutive ordinals, every below-the-line asset
    is selected at least once — the anti-starvation guarantee that the pure
    top-cap selection could never make."""
    import math

    ranked = _ranked_assets(23)  # cap=10, slots=4 -> guaranteed 6, remainder R=17
    remainder_ids = {a["id"] for a in ranked[6:]}
    window = math.ceil(len(remainder_ids) / 4)  # 5 runs
    seen: set = set()
    for ordinal in range(1, window + 1):
        out = VulnAgent._apply_cap_with_rotation(ranked, cap=10, slots=4, run_ordinal=ordinal)
        seen |= {a["id"] for a in out}
    assert remainder_ids <= seen


def test_rotation_slots_clamped_to_cap():
    """slots > cap degrades to a fully rotating budget, never an oversized one."""
    ranked = _ranked_assets(9)
    out = VulnAgent._apply_cap_with_rotation(ranked, cap=3, slots=99, run_ordinal=1)
    assert len(out) == 3
    # slots clamped to cap=3, guaranteed slice empty, offset = (1*3) % 9 = 3
    assert [a["id"] for a in out] == [3, 4, 5]


def test_bounded_assets_rotation_selects_different_window_per_ordinal(sqlite_engine):
    """Integration through _bounded_assets: a real settings int for
    rotation_slots + a stubbed ordinal changes WHICH below-the-line assets are
    selected while keeping the guaranteed top and the truncation audit row
    (now carrying rotation_slots/run_ordinal)."""
    import json
    from contextlib import contextmanager

    from infra_brain.db.models import AgentActionLog

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = MagicMock()
    agent.settings.rapid7_vuln_asset_cap = 4
    agent.settings.rapid7_vuln_rotation_slots = 2
    agent.callbacks = []
    agent._active_run_id = uuid.uuid4()

    assets = _ranked_assets(8)  # cap=4, slots=2 -> guaranteed {0,1}, remainder R=6

    with patch("infra_brain.agents.vuln.get_session", _get_session):
        with patch.object(VulnAgent, "_coverage_rotation_ordinal", return_value=1):
            run1 = agent._bounded_assets(list(assets))
        with patch.object(VulnAgent, "_coverage_rotation_ordinal", return_value=2):
            run2 = agent._bounded_assets(list(assets))

    ids1 = [a["id"] for a in run1]
    ids2 = [a["id"] for a in run2]
    assert ids1[:2] == [0, 1] and ids2[:2] == [0, 1]  # guaranteed top always in
    assert len(run1) == len(run2) == 4
    assert ids1[2:] != ids2[2:]  # rotation window advanced between ordinals
    # offset advances by slots per ordinal over remainder [2..7]:
    assert ids1[2:] == [4, 5] and ids2[2:] == [6, 7]

    with Session(sqlite_engine) as s:
        rows = s.query(AgentActionLog).filter_by(tool="truncation").all()
        assert len(rows) == 2
        summary = json.loads(rows[0].args_summary)
        assert summary["entity"] == "rapid7_vuln_asset_cap"
        assert summary["rotation_slots"] == 2
        assert summary["run_ordinal"] == 1


def test_coverage_rotation_ordinal_counts_non_skipped_runs(sqlite_engine):
    """The ordinal is the domain's non-skipped collection_runs count; skipped
    rows (Rapid7 unconfigured) don't advance the window, other domains don't
    leak in, and a read failure degrades to 0 (old behavior) not an error."""
    from contextlib import contextmanager

    from infra_brain.db.models import CollectionRun

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    with Session(sqlite_engine) as s:
        s.add(CollectionRun(domain="vuln", trigger_type="scheduled", status="completed"))
        s.add(CollectionRun(domain="vuln", trigger_type="scheduled", status="in_progress"))
        s.add(CollectionRun(domain="vuln", trigger_type="scheduled", status="skipped"))
        s.add(CollectionRun(domain="netdiscovery", trigger_type="scheduled", status="completed"))
        s.commit()

    agent = VulnAgent.__new__(VulnAgent)
    with patch("infra_brain.agents.vuln.get_session", _get_session):
        assert agent._coverage_rotation_ordinal() == 2

    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with patch("infra_brain.agents.vuln.get_session", _boom):
        assert agent._coverage_rotation_ordinal() == 0
