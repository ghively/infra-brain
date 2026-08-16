"""Tests for SaaSInventoryAgent (GitLab #103)."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.saas_inventory import SaaSInventoryAgent
from infra_brain.etl.base import CollectorSkipped
from infra_brain.db.models import Resource, SaaSApiKeyMetadata, SaaSApplication

MOCK_APPS = [
    {"name": "Acme SaaS", "vendor": "Acme", "category": "productivity", "owner_team": "team-infra"}
]
MOCK_KEYS = [
    {
        "key_name": "ci-deploy-key",
        "scope": "read",
        "created_at": "2026-07-01T00:00:00Z",
        "last_used_at": "2026-07-20T00:00:00Z",
        "owner": "operator",
        "is_active": True,
    }
]


def _settings(admin_url="https://saas-admin.example.com/api"):
    return SimpleNamespace(saas_admin_url=admin_url)


class TestDomain:
    def test_domain_is_set(self, make_agent):
        assert make_agent(SaaSInventoryAgent).domain == "saas_inventory"


class TestCollectSuccess:
    def test_collect_success(self, make_agent):
        agent = make_agent(SaaSInventoryAgent, settings=_settings())

        with (
            patch("infra_brain.agents.saas_inventory.saas_applications_tool") as mock_apps,
            patch("infra_brain.agents.saas_inventory.saas_api_keys_tool") as mock_keys,
        ):
            mock_apps.invoke.return_value = MOCK_APPS
            mock_keys.invoke.return_value = MOCK_KEYS
            outcome = agent.collect(scope="all")

        assert outcome.errors == []
        assert len(outcome.items) == 1
        item = outcome.items[0]
        assert item["type"] == "saas_application"
        assert item["name"] == "Acme SaaS"
        assert item["data"]["vendor"] == "Acme"
        assert agent._last_keys["Acme SaaS"] == MOCK_KEYS


class TestCollectEmpty:
    def test_collect_empty_when_no_apps(self, make_agent):
        agent = make_agent(SaaSInventoryAgent, settings=_settings())

        with patch("infra_brain.agents.saas_inventory.saas_applications_tool") as mock_apps:
            mock_apps.invoke.return_value = []
            outcome = agent.collect(scope="all")

        assert outcome.items == []
        assert outcome.errors == []

    def test_collect_self_skips_when_not_configured(self, make_agent):
        """Empty config must self-skip — no tool call, and status="skipped".

        This previously returned an empty CollectOutcome ("clean no-op"), which
        BaseAgent records as status="completed" with 0 rows. The R3
        completeness monitor correctly reads that as "last completed run wrote
        zero rows (silently empty?)" and had escalated it 168x in a row — for
        an integration that was simply never configured. "skipped" is the
        outcome that describes an absent dependency, and it is what every
        sibling collector raises.
        """
        agent = make_agent(SaaSInventoryAgent, settings=_settings(admin_url=""))

        with patch("infra_brain.agents.saas_inventory.saas_applications_tool") as mock_apps:
            with pytest.raises(CollectorSkipped, match="saas_admin_url not configured"):
                agent.collect(scope="all")

        mock_apps.invoke.assert_not_called()


class TestCollectException:
    def test_collect_raises_on_api_error_does_not_propagate(self, make_agent):
        """collect() itself must not raise — errors are surfaced via CollectOutcome."""
        agent = make_agent(SaaSInventoryAgent, settings=_settings())

        with patch("infra_brain.agents.saas_inventory.saas_applications_tool") as mock_apps:
            mock_apps.invoke.side_effect = RuntimeError("SaaS admin API down")
            outcome = agent.collect(scope="all")

        assert outcome.items == []
        assert len(outcome.errors) == 1
        assert "SaaS admin API down" in outcome.errors[0]

    def test_run_reports_failed_on_collect_exception(self, make_agent, sqlite_engine):
        """BaseAgent/ETLConnector.run() catches exceptions raised out of collect()
        and records status="failed" on the CollectionRun row (F-007: never silent)."""
        run_settings = _settings()
        run_settings.collection_disabled_domains = ""
        agent = make_agent(SaaSInventoryAgent, settings=run_settings)

        @contextmanager
        def _get_session():
            with Session(sqlite_engine) as s:
                yield s

        with (
            patch("infra_brain.etl.base.get_session", _get_session),
            patch.object(agent, "collect", side_effect=RuntimeError("boom")),
        ):
            result = agent.run()
        assert result.status == "failed"
        assert "boom" in result.errors[0]


class TestNeverPersistsKeyValue:
    def test_write_saas_details_never_persists_secret_value(
        self, make_agent, sqlite_engine, session_patcher
    ):
        """Even if a raw dict carrying a `value` field slipped past the tool
        boundary, the agent's own metadata-only allowlist must never persist it.
        """
        agent = make_agent(SaaSInventoryAgent, settings=_settings())
        agent._last_apps = MOCK_APPS
        agent._last_keys = {
            "Acme SaaS": [
                {
                    "key_name": "ci-deploy-key",
                    "scope": "read",
                    "value": "sk_live_super_secret_value",  # must never persist
                    "secret": "another_secret",
                }
            ]
        }

        with Session(sqlite_engine) as seed:
            seed.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="saas_inventory",
                    type="saas_application",
                    name="Acme SaaS",
                    source="saas_inventory",
                )
            )
            seed.commit()

        with session_patcher("infra_brain.agents.saas_inventory"):
            count = agent._write_saas_details(scope="all")

        assert isinstance(count, int)
        assert count > 0

        with Session(sqlite_engine) as v:
            app_row = v.query(SaaSApplication).filter_by(name="Acme SaaS").one()
            key_row = v.query(SaaSApiKeyMetadata).filter_by(app_name="Acme SaaS").one()

        assert not hasattr(key_row, "value")
        assert not hasattr(key_row, "secret")
        # No column anywhere on the persisted rows may contain the secret string.
        for obj in (app_row, key_row):
            for col in obj.__table__.columns:
                val = getattr(obj, col.name)
                assert "sk_live_super_secret_value" not in str(val)
                assert "another_secret" not in str(val)
