"""Tests for DnsAgent — DNS zone/record collection (GitLab issue #95).

Covers: success (mocked SOA + AXFR), empty/unconfigured (CollectorSkipped),
and exception handling (SOA query failure -> partial/failed CollectOutcome,
dnspython unavailable -> CollectorSkipped).
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.dns import DnsAgent
from infra_brain.db.models import AgentActionLog, DnsRecord, DnsZone
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.dns"

_SOA = {
    "serial": 2026073001,
    "primary_ns": "ns1.example.com",
    "admin_email": "hostmaster.example.com",
    "refresh_seconds": 3600,
    "retry_seconds": 600,
    "expire_seconds": 604800,
    "minimum_ttl_seconds": 300,
}

_RECORDS = [
    {"name": "www.example.com", "type": "A", "value": "10.0.0.1", "ttl": 300, "priority": None},
    {"name": "mail.example.com", "type": "A", "value": "10.0.0.2", "ttl": 300, "priority": None},
    {
        "name": "example.com",
        "type": "MX",
        "value": "mail.example.com",
        "ttl": 300,
        "priority": 10,
    },
]


def _settings(zones="example.com", resolver="10.0.0.53", timeout=5, allowed_zones=""):
    from unittest.mock import MagicMock

    s = MagicMock()
    s.dns_zones = zones
    s.dns_resolver_host = resolver
    s.dns_query_timeout_seconds = timeout
    s.dns_allowed_zones = allowed_zones
    return s


# --- success case ------------------------------------------------------------


def test_collect_success_with_axfr(make_agent, session_patcher):
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)) as mock_soa,
            patch(f"{MODULE}.query_axfr", return_value=list(_RECORDS)) as mock_axfr,
        ):
            result = agent.collect()
            mock_soa.assert_called_once_with("example.com", "10.0.0.53", 5.0)
            mock_axfr.assert_called_once_with("example.com", "10.0.0.53", 5.0)

        assert isinstance(result, CollectOutcome)
        assert result.status == "ok"
        assert len(result.items) == 1
        item = result.items[0]
        assert item["name"] == "example.com"
        assert item["type"] == "dns_zone"
        assert item["data"]["serial"] == _SOA["serial"]
        assert item["data"]["axfr_succeeded"] is True
        assert item["data"]["record_count"] == 3
        assert agent._zone_records == {"example.com": _RECORDS}

        # Generic Resource/Snapshot upsert happens in run(), not collect() —
        # simulate it here so the detail-write phase has a Resource to key on.
        from infra_brain.api._seeding import upsert_resource

        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=item["data"],
            )
            s.commit()

        written = agent._write_dns_zones()
        assert written == 1 + 3  # 1 zone + 3 records

        with Session(engine) as s:
            zone = s.query(DnsZone).filter_by(name="example.com").one()
            assert zone.serial == _SOA["serial"]
            assert zone.axfr_succeeded is True
            records = s.query(DnsRecord).filter_by(zone_id=zone.id).all()
            assert len(records) == 3
            values = {(r.name, r.type, r.value) for r in records}
            assert ("www.example.com", "A", "10.0.0.1") in values
            assert ("example.com", "MX", "mail.example.com") in values


def test_write_dns_zones_is_idempotent(make_agent, session_patcher):
    """Re-running the detail-write phase upserts in place — no duplicate rows."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=list(_RECORDS)),
        ):
            result = agent.collect()

        from infra_brain.api._seeding import upsert_resource

        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=result.items[0]["data"],
            )
            s.commit()

        agent._write_dns_zones()
        agent._write_dns_zones()

        with Session(engine) as s:
            assert s.query(DnsZone).count() == 1
            assert s.query(DnsRecord).count() == 3


# --- record pruning (L-2) ------------------------------------------------------


def test_write_dns_zones_prunes_record_absent_from_successful_axfr(make_agent, session_patcher):
    """A record that disappears upstream (deleted zone entry) must be pruned
    from DnsRecord on the next SUCCESSFUL AXFR — this is the stale-record
    case DnsAgent exists to catch. Without pruning the row lives forever."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        from infra_brain.api._seeding import upsert_resource

        # First run: 3 records land, including www.example.com.
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=list(_RECORDS)),
        ):
            result = agent.collect()
        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=result.items[0]["data"],
            )
            s.commit()
        agent._write_dns_zones()

        with Session(engine) as s:
            assert s.query(DnsRecord).count() == 3

        # Second run: www.example.com is gone upstream (deleted record) —
        # AXFR succeeds and returns only the remaining 2 records.
        remaining = [r for r in _RECORDS if r["name"] != "www.example.com"]
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=list(remaining)),
        ):
            result2 = agent.collect()
        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=result2.items[0]["data"],
            )
            s.commit()
        agent._write_dns_zones()

        with Session(engine) as s:
            names = {r.name for r in s.query(DnsRecord).all()}
            assert names == {"mail.example.com", "example.com"}
            assert "www.example.com" not in names


def test_write_dns_zones_does_not_prune_on_failed_axfr(make_agent, session_patcher):
    """When AXFR fails (reduced fidelity, not authoritative absence), existing
    DnsRecord rows from a prior successful fetch must be left alone — pruning
    is only safe within the scope actually successfully observed this run."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        from infra_brain.api._seeding import upsert_resource

        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=list(_RECORDS)),
        ):
            result = agent.collect()
        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=result.items[0]["data"],
            )
            s.commit()
        agent._write_dns_zones()

        with Session(engine) as s:
            assert s.query(DnsRecord).count() == 3

        # Second run: AXFR refused this cycle — must NOT wipe the 3 records
        # collected last time even though this run's stash is empty.
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", side_effect=RuntimeError("REFUSED")),
        ):
            result2 = agent.collect()
        assert result2.items[0]["data"]["axfr_succeeded"] is False
        with Session(engine) as s:
            upsert_resource(
                s,
                name="example.com",
                domain="dns",
                resource_type="dns_zone",
                metadata=result2.items[0]["data"],
            )
            s.commit()
        agent._write_dns_zones()

        with Session(engine) as s:
            assert s.query(DnsRecord).count() == 3


# --- empty / unconfigured -----------------------------------------------------


def test_collect_skipped_when_no_zones_configured(make_agent):
    agent = make_agent(DnsAgent, settings=_settings(zones=""))
    with pytest.raises(CollectorSkipped, match="dns_zones"):
        agent.collect()


def test_collect_skipped_when_dnspython_unavailable(make_agent):
    agent = make_agent(DnsAgent, settings=_settings())
    with patch(f"{MODULE}._DNSPYTHON_AVAILABLE", False):
        with pytest.raises(CollectorSkipped, match="dnspython"):
            agent.collect()


def test_write_dns_zones_noop_when_nothing_collected(make_agent):
    agent = make_agent(DnsAgent, settings=_settings())
    # _zone_records never set (collect() never ran) — must not raise.
    written = agent._write_dns_zones()
    assert written == 0


# --- exception handling --------------------------------------------------------


def test_collect_partial_when_axfr_fails_but_soa_succeeds(make_agent):
    """AXFR failure is expected/common (server refuses transfer) — must NOT
    surface as a collect() error; the zone item still reports with SOA-only
    data and axfr_succeeded=False."""
    agent = make_agent(DnsAgent, settings=_settings())
    with (
        patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
        patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
        patch(f"{MODULE}.query_axfr", side_effect=RuntimeError("REFUSED")),
    ):
        result = agent.collect()

    assert result.status == "ok"
    assert len(result.items) == 1
    assert result.items[0]["data"]["axfr_succeeded"] is False
    assert result.items[0]["data"]["record_count"] == 0
    assert agent._zone_records == {"example.com": []}


def test_collect_failed_when_soa_query_raises_for_every_zone(make_agent):
    agent = make_agent(DnsAgent, settings=_settings(zones="example.com,other.com"))
    with (
        patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
        patch(f"{MODULE}.query_soa", side_effect=RuntimeError("NXDOMAIN")),
    ):
        result = agent.collect()

    assert isinstance(result, CollectOutcome)
    assert result.status == "failed"
    assert result.items == []
    assert result.count_override == 0
    assert len(result.errors) == 2


def test_collect_partial_when_one_of_two_zones_fails(make_agent):
    agent = make_agent(DnsAgent, settings=_settings(zones="good.com,bad.com"))

    def _soa_side_effect(zone_name, resolver_host, timeout):
        if zone_name == "bad.com":
            raise RuntimeError("timed out")
        return dict(_SOA)

    with (
        patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
        patch(f"{MODULE}.query_soa", side_effect=_soa_side_effect),
        patch(f"{MODULE}.query_axfr", return_value=[]),
    ):
        result = agent.collect()

    assert result.status == "partial"
    assert len(result.items) == 1
    assert result.items[0]["name"] == "good.com"
    assert len(result.errors) == 1
    assert "bad.com" in result.errors[0]


# --- allow-list gate (residual 1, GitLab #95) ----------------------------------


def test_gate_denies_zone_not_in_allowlist(make_agent, session_patcher):
    """A zone that fails dns_allowed_zones must be denied BEFORE any SOA/AXFR
    query and must write a `deny` AgentActionLog row — mirroring
    NetDiscoveryAgent._gate_nmap_targets' fail-closed, audit-then-raise shape."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(
            DnsAgent, settings=_settings(zones="example.com", allowed_zones="other.com")
        )
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa") as mock_soa,
            patch(f"{MODULE}.query_axfr") as mock_axfr,
        ):
            result = agent.collect()
            mock_soa.assert_not_called()
            mock_axfr.assert_not_called()

        assert isinstance(result, CollectOutcome)
        assert result.items == []
        assert len(result.errors) == 1
        assert "example.com" in result.errors[0]
        assert "denied" in result.errors[0]

        with Session(engine) as s:
            rows = s.query(AgentActionLog).filter_by(tool="dns_gate").all()
            assert len(rows) == 1
            assert rows[0].verdict == "deny"
            assert rows[0].status == "error"


def test_gate_allows_all_zones_when_unset(make_agent, session_patcher):
    """Empty dns_allowed_zones (default) must allow every configured zone —
    backward compatible with pre-gate behavior."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(
            DnsAgent, settings=_settings(zones="example.com", allowed_zones="")
        )
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)) as mock_soa,
            patch(f"{MODULE}.query_axfr", return_value=[]) as mock_axfr,
        ):
            result = agent.collect()
            mock_soa.assert_called_once()
            mock_axfr.assert_called_once()

        assert result.status == "ok"
        assert len(result.items) == 1

        with Session(engine) as s:
            deny_rows = s.query(AgentActionLog).filter_by(tool="dns_gate").all()
            assert deny_rows == []


# --- AXFR TXT DLP boundary (residual 2, GitLab #95) -----------------------------

_PAN_TXT_RECORD = {
    "name": "spf-note.example.com",
    "type": "TXT",
    "value": "card on file: 4111111111111111",
    "ttl": 300,
    "priority": None,
}


def test_axfr_txt_dlp_raises_when_fail_closed(make_agent, session_patcher):
    """A DLP-flagged TXT value (Luhn-valid PAN) must be caught by
    DLPCallbackHandler._check BEFORE it reaches self._zone_records — under
    dlp_fail_closed=True this degrades the zone to SOA-only data, the same
    fail-closed shape AXFR-refused already produces."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=[dict(_PAN_TXT_RECORD)]),
            patch("infra_brain.callbacks.dlp.get_settings") as mock_dlp_settings,
            patch("infra_brain.callbacks.dlp.get_session") as mock_dlp_get_session,
        ):
            mock_dlp_settings.return_value.dlp_fail_closed = True
            mock_dlp_get_session.return_value.__enter__ = lambda s: Session(engine)
            mock_dlp_get_session.return_value.__exit__ = lambda *a: False

            result = agent.collect()

        assert result.status == "ok"
        assert len(result.items) == 1
        # AXFR degraded to SOA-only: the DLP raise inside the try/except is
        # treated the same as "AXFR unavailable" — no flagged TXT value ever
        # reaches the stash.
        assert result.items[0]["data"]["axfr_succeeded"] is False
        assert result.items[0]["data"]["record_count"] == 0
        assert agent._zone_records == {"example.com": []}


def test_axfr_txt_dlp_logs_when_fail_open(make_agent, session_patcher):
    """Under dlp_fail_closed=False the DLP check logs (does not raise) — the
    flagged TXT record still flows through, matching DLPCallbackHandler's
    documented permissive-mode behavior for any other tool boundary."""
    with session_patcher(MODULE) as engine:
        agent = make_agent(DnsAgent, settings=_settings())
        with (
            patch(f"{MODULE}._DNSPYTHON_AVAILABLE", True),
            patch(f"{MODULE}.query_soa", return_value=dict(_SOA)),
            patch(f"{MODULE}.query_axfr", return_value=[dict(_PAN_TXT_RECORD)]),
            patch("infra_brain.callbacks.dlp.get_settings") as mock_dlp_settings,
            patch("infra_brain.callbacks.dlp.get_session") as mock_dlp_get_session,
        ):
            mock_dlp_settings.return_value.dlp_fail_closed = False
            mock_dlp_get_session.return_value.__enter__ = lambda s: Session(engine)
            mock_dlp_get_session.return_value.__exit__ = lambda *a: False

            result = agent.collect()

        assert result.status == "ok"
        assert result.items[0]["data"]["axfr_succeeded"] is True
        assert result.items[0]["data"]["record_count"] == 1
        assert agent._zone_records == {"example.com": [_PAN_TXT_RECORD]}
