"""Shared fixtures for the agent test suite.

Removes the ``Agent.__new__(Agent)`` + ``MagicMock`` settings boilerplate that was
duplicated across every per-agent test module, and the in-memory-SQLite setup the
DB-backed agents (compliance, rootcause, vuln_triage, inventory_reconcile, ...) all
repeat. Adoption is incremental: module-local ``_make_agent`` / ``engine`` helpers
continue to work, so existing tests are untouched until they choose to migrate.
``test_octopus.py`` and ``test_cloud.py`` are the reference adopters.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from tests.support.pg import make_engine


@pytest.fixture
def make_agent():
    """Factory that builds a tool-based agent without running ``BaseAgent.__init__``.

    Mirrors the ``Agent.__new__(Agent)`` + ``settings`` / ``callbacks`` setup used
    throughout the suite. ``settings`` defaults to a ``MagicMock`` and ``callbacks``
    to an empty list; pass either to override.

        agent = make_agent(OctopusAgent)
        agent = make_agent(CloudAgent, settings=my_settings)
    """

    def _factory(agent_cls, settings=None, callbacks=None):
        agent = agent_cls.__new__(agent_cls)
        agent.settings = MagicMock() if settings is None else settings
        agent.callbacks = [] if callbacks is None else callbacks
        return agent

    return _factory


@pytest.fixture
def orm_engine():
    """Empty engine with the full ORM schema created.

    In-memory SQLite by default; a real PostgreSQL when ``PG_GATE_DSN`` is set
    (the ``agent-orm-check`` gate, TRK-356). See ``tests/support/pg.py``.
    """
    return make_engine()


@pytest.fixture
def sqlite_engine(orm_engine):
    """Backwards-compatible alias for :func:`orm_engine`.

    Kept because ~30 modules already ask for ``sqlite_engine`` by name. The
    name is now a slight lie in gate mode — prefer ``orm_engine`` in new tests.
    """
    return orm_engine


@pytest.fixture
def session_patcher(orm_engine):
    """Patch ``get_session`` in an agent module to use the test engine.

        with session_patcher("infra_brain.agents.compliance"):
            ComplianceAgent().collect()

    Yields the engine so the test can open its own ``Session`` to seed/assert.
    """

    @contextmanager
    def _get_session():
        with Session(orm_engine) as s:
            yield s

    @contextmanager
    def _patch(module_path):
        with patch(f"{module_path}.get_session", _get_session):
            yield orm_engine

    return _patch
