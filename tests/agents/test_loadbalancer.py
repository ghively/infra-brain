"""Tests for LoadBalancerAgent (GitLab issue #100)."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from infra_brain.agents.loadbalancer import LoadBalancerAgent
from infra_brain.etl.base import CollectorSkipped

from tests.support.pg import make_engine


def _engine():
    eng = make_engine()
    return eng


def _session_factory(eng):
    factory = sessionmaker(bind=eng)

    @contextmanager
    def _get():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _get


def _agent(**settings):
    """Build a LoadBalancerAgent without running ETLConnector.__init__ (no DB/LLM needed)."""
    agent = LoadBalancerAgent.__new__(LoadBalancerAgent)
    defaults = dict(
        lb_enabled=False,
        f5_host="",
        f5_username="",
        f5_password="",
        f5_ssl_verify=True,
        nginx_plus_url="",
        haproxy_stats_url="",
        haproxy_stats_username="",
        haproxy_stats_password="",
        cloudflare_api_token="",
        cloudflare_zone_ids="",
        collection_disabled_domains="",
        collect_timeout_seconds=300,
    )
    defaults.update(settings)
    agent.settings = SimpleNamespace(**defaults)
    agent.callbacks = []
    return agent


class TestCollectSkipped:
    def test_collect_raises_skipped_when_disabled(self):
        """lb_enabled=False -> CollectorSkipped (skipped is distinct from working-empty)."""
        with pytest.raises(CollectorSkipped):
            _agent().collect("all")


class TestCollectEmpty:
    def test_collect_returns_empty_when_enabled_but_no_vendor_configured(self):
        """lb_enabled=True but every vendor left blank -> [] (nothing fabricated)."""
        outcome = _agent(lb_enabled=True).collect("all")
        assert outcome.items == []
        assert outcome.errors == []


class TestCollectSuccess:
    def test_collect_haproxy_success(self):
        """A configured vendor (HAProxy here) yields real lb_instance + lb_pool items."""
        agent = _agent(lb_enabled=True, haproxy_stats_url="http://lb1/stats")
        fake_pool = {
            "name": "web_backend",
            "type": "lb_pool",
            "data": {
                "vendor": "haproxy",
                "instance": "http://lb1/stats",
                "monitor_type": "http",
                "members": [{"address": "10.0.0.5", "port": 8080, "state": "up", "weight": 1}],
            },
        }
        with patch("infra_brain.agents.loadbalancer.haproxy_stats_tool") as mock_tool:
            mock_tool.invoke.return_value = [fake_pool]
            outcome = agent.collect("all")

        assert isinstance(outcome.items, list)
        assert any(i["type"] == "lb_instance" for i in outcome.items)
        assert fake_pool in outcome.items
        assert outcome.errors == []
        mock_tool.invoke.assert_called_once()

    def test_collect_cloudflare_normalizes_instance_keys_to_zone_name(self):
        """Regression test for the Cloudflare per-zone "instance" key mismatch.

        cloudflare_zones_tool creates the real, zone-domain-keyed
        "lb_instance" item; cloudflare_load_balancers_tool only knows the
        zone_id and cloudflare_pools_tool is account-scoped ("cloudflare"
        literal) — collect() must normalize both so every item's
        ``data["instance"]`` matches a real "lb_instance" item's ``name``,
        otherwise _get_or_create_instance()'s (vendor, name) lookup
        fabricates a disconnected orphan LbInstance per cycle.
        """
        agent = _agent(lb_enabled=True, cloudflare_api_token="tok")
        fake_zone = {
            "name": "example.com",
            "type": "lb_instance",
            "data": {"vendor": "cloudflare", "zone_id": "abc123zoneid"},
        }
        fake_lb = {
            "name": "www-lb",
            "type": "lb_virtual_server",
            "data": {
                "vendor": "cloudflare",
                "instance": "abc123zoneid",  # what the raw tool returns
                "pool": "origin-pool",
                "protocol": "https",
                "tls_enabled": True,
            },
        }
        fake_pool = {
            "name": "origin-pool",
            "type": "lb_pool",
            "data": {
                "vendor": "cloudflare",
                "instance": "cloudflare",
                "monitor_type": "http",
                "members": [],
            },
        }
        with (
            patch("infra_brain.agents.loadbalancer.cloudflare_zones_tool") as mock_zones,
            patch("infra_brain.agents.loadbalancer.cloudflare_load_balancers_tool") as mock_lbs,
            patch("infra_brain.agents.loadbalancer.cloudflare_pools_tool") as mock_pools,
        ):
            mock_zones.invoke.return_value = [fake_zone]
            mock_lbs.invoke.return_value = [fake_lb]
            mock_pools.invoke.return_value = [fake_pool]
            outcome = agent.collect("all")

        mock_lbs.invoke.assert_called_once_with(
            {"zone_id": "abc123zoneid"}, config={"callbacks": agent.callbacks}
        )

        instance_items = {i["name"] for i in outcome.items if i["type"] == "lb_instance"}
        # the zone's lb_instance (example.com) and the account-level
        # cloudflare pools' lb_instance ("cloudflare") must both be present.
        assert instance_items == {"example.com", "cloudflare"}

        vs_item = next(i for i in outcome.items if i["type"] == "lb_virtual_server")
        # normalized from the raw zone_id to the real zone-name instance key.
        assert vs_item["data"]["instance"] == "example.com"

        pool_item = next(i for i in outcome.items if i["type"] == "lb_pool")
        assert pool_item["data"]["instance"] == "cloudflare"
        assert outcome.errors == []

    def test_collect_multiple_vendors_aggregate(self):
        """Two vendors configured at once both contribute items."""
        agent = _agent(
            lb_enabled=True,
            nginx_plus_url="http://nginx1/api/8",
            haproxy_stats_url="http://lb1/stats",
        )
        with (
            patch("infra_brain.agents.loadbalancer.nginx_plus_upstreams_tool") as mock_nginx,
            patch("infra_brain.agents.loadbalancer.haproxy_stats_tool") as mock_haproxy,
        ):
            mock_nginx.invoke.return_value = []
            mock_haproxy.invoke.return_value = []
            outcome = agent.collect("all")

        instance_names = {i["name"] for i in outcome.items if i["type"] == "lb_instance"}
        assert instance_names == {"http://nginx1/api/8", "http://lb1/stats"}


class TestCollectException:
    def test_vendor_failure_recorded_as_error_not_raised(self):
        """One vendor raising must not blow up collect() — recorded as an error (F-007)."""
        agent = _agent(lb_enabled=True, haproxy_stats_url="http://lb1/stats")
        with patch("infra_brain.agents.loadbalancer.haproxy_stats_tool") as mock_tool:
            mock_tool.invoke.side_effect = RuntimeError("connection refused")
            outcome = agent.collect("all")

        # the lb_instance seed item is appended before the failing tool call,
        # so it survives; the pool/vs data the tool would have added does not.
        assert outcome.items == [
            {
                "name": "http://lb1/stats",
                "type": "lb_instance",
                "data": {"vendor": "haproxy", "instance": "http://lb1/stats"},
            }
        ]
        assert any("connection refused" in e for e in outcome.errors)

    def test_collect_exception_does_not_propagate_via_run(self):
        """ETLConnector.run() catches exceptions raised out of collect() entirely."""
        agent = _agent(lb_enabled=True)
        eng = _engine()
        with (
            patch("infra_brain.etl.base.get_session", _session_factory(eng)),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
            patch.object(agent, "collect", side_effect=RuntimeError("upstream down")),
        ):
            run_result = agent.run()
        assert run_result.status == "failed"
        assert "upstream down" in run_result.errors[0]


class TestWriteLbDetailsSkipReporting:
    def test_every_detail_item_failing_downgrades_run_to_partial(self):
        """M-2 (F-007): ``_write_lb_details``'s per-item SAVEPOINT
        (``session.begin_nested()``) logs and skips a bad LB item so its
        siblings still commit — but that per-item resilience swallowed the
        failure entirely: it never reached the run's ``CollectionResult``, so
        a detail-write phase that dropped EVERY item still reported
        "completed" with an empty ``errors`` list on both the returned
        ``CollectionResult`` and the persisted ``CollectionRun`` row.
        """
        from infra_brain.etl.base import CollectOutcome

        agent = _agent(lb_enabled=True, default_zone=None)
        eng = _engine()
        items = [
            {"type": "lb_pool", "name": "pool-1", "data": {"vendor": "f5"}},
            {"type": "lb_pool", "name": "pool-2", "data": {"vendor": "f5"}},
        ]
        # collect() is mocked below (no real vendor config), so its own
        # `self._last_items = items` assignment never runs — set it directly,
        # matching what a real collect() call would have left behind.
        agent._last_items = items

        with (
            patch("infra_brain.etl.base.get_session", _session_factory(eng)),
            patch("infra_brain.agents.loadbalancer.get_session", _session_factory(eng)),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
            patch.object(agent, "collect", return_value=CollectOutcome(items=items, errors=[])),
            patch.object(LoadBalancerAgent, "_upsert_pool", side_effect=RuntimeError("boom")),
        ):
            result = agent.run()

        assert result.status != "completed", (
            "a run that dropped every LB detail item must never report 'completed'"
        )
        assert result.errors, "the dropped items must be recorded in result.errors"
        assert any("pool-1" in e for e in result.errors)
        assert any("pool-2" in e for e in result.errors)

        from sqlalchemy.orm import Session

        from infra_brain.db.models import CollectionRun

        with Session(eng) as s:
            run = s.get(CollectionRun, result.run_id)
            assert run is not None
            assert run.status != "completed", (
                "the persisted CollectionRun row must also reflect the dropped work"
            )


class TestDomainAndSpec:
    def test_domain_is_set(self):
        assert LoadBalancerAgent.spec.domain == "loadbalancer"

    def test_callbacks_wired(self):
        """build_callbacks() must be called on a real instance — safety layer must be active."""
        agent = _agent()
        assert agent.callbacks is not None
        assert isinstance(agent.callbacks, list)
