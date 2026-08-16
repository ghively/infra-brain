"""Tests for collection_disabled_domains feature (F-022).

Verifies that ETLConnector.run() raises CollectorSkipped (which BaseAgent.run()
then records as status="skipped") when the agent's domain is listed in the
collection_disabled_domains setting. Domains not in the list run normally.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import CollectionRun
from infra_brain.etl.base import ETLConnector

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# Minimal concrete ETLConnector for testing
# ---------------------------------------------------------------------------


class _DomainAgent(ETLConnector):
    """Minimal concrete subclass: always returns one dummy item."""

    def __init__(self, domain: str, disabled_domains: str = ""):
        self.domain = domain  # override class attribute
        self.settings = MagicMock()
        self.settings.collection_disabled_domains = disabled_domains
        self.settings.collect_timeout_seconds = 30
        self.settings.default_zone = "corpor"  # must be a real str for DB insert
        self.callbacks = []

    def collect(self, scope: str = "all") -> list[dict]:
        return [{"name": "test-resource", "type": "test_type", "data": {}}]


# ---------------------------------------------------------------------------
# Shared SQLite in-memory engine for CollectionRun persistence
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = make_engine()
    return eng


def _session_factory(engine):
    @contextmanager
    def _cm():
        s = Session(engine)
        try:
            yield s
        finally:
            s.close()

    return _cm


# ---------------------------------------------------------------------------
# Domain-disabled tests: CollectorSkipped is raised; DB records "skipped"
# ---------------------------------------------------------------------------


class TestCollectionDisabledDomains:
    def test_disabled_domain_raises_collector_skipped(self, engine):
        """A domain in collection_disabled_domains must produce status='skipped'."""
        agent = _DomainAgent(domain="vsphere", disabled_domains="vsphere,windows")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "skipped"
        assert result.resources_found == 0
        # CollectorSkipped writes reason to CollectionRun.error_message, not result.errors.
        with Session(engine) as s:
            run = s.get(CollectionRun, result.run_id)
            assert run is not None
            assert "collection_disabled_domains" in (run.error_message or "")

    def test_disabled_domain_db_row_is_skipped(self, engine):
        """CollectionRun row must be status='skipped' when domain is disabled."""
        agent = _DomainAgent(domain="vsphere", disabled_domains="vsphere")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        with Session(engine) as s:
            run = s.get(CollectionRun, result.run_id)
            assert run is not None
            assert run.status == "skipped"
            assert run.error_message is not None
            assert "vsphere" in run.error_message

    def test_non_disabled_domain_runs_normally(self, engine):
        """A domain NOT in collection_disabled_domains must run and complete."""
        agent = _DomainAgent(domain="linux", disabled_domains="vsphere,windows")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "completed"
        assert result.resources_found == 1

    def test_empty_disabled_domains_all_run(self, engine):
        """Empty collection_disabled_domains means all domains are enabled."""
        agent = _DomainAgent(domain="vsphere", disabled_domains="")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "completed"

    def test_whitespace_in_disabled_list_is_stripped(self, engine):
        """Whitespace around domain names in the comma-separated list is stripped."""
        agent = _DomainAgent(domain="vsphere", disabled_domains="  linux , vsphere , windows  ")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "skipped"

    def test_case_sensitive_match(self, engine):
        """Domain matching is case-sensitive: 'VSphere' != 'vsphere'."""
        agent = _DomainAgent(domain="vsphere", disabled_domains="VSphere,Windows")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        # 'vsphere' (lower) is NOT in ['VSphere', 'Windows'] → runs normally
        assert result.status == "completed"

    def test_partial_name_not_matched(self, engine):
        """Partial domain name does not disable the full domain."""
        # 'vsph' is listed but agent domain is 'vsphere' — must NOT match
        agent = _DomainAgent(domain="vsphere", disabled_domains="vsph")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "completed"

    def test_single_disabled_domain(self, engine):
        """Single domain (no commas) in disabled list is parsed correctly."""
        agent = _DomainAgent(domain="windows", disabled_domains="windows")
        sf = _session_factory(engine)
        with (
            patch("infra_brain.etl.base.get_session", sf),
            patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        ):
            result = agent.run()

        assert result.status == "skipped"
