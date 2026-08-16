"""Runtime-flippable collector dispatchable toggles (TRK-303 part 5/7).

dispatch() consults a live ``dispatchable__<domain>`` RuntimeConfig override
before running a normally-dispatchable agent. This is a separate,
runtime-flippable layer on top of AgentSpec.dispatchable (which still governs
AGENT_REGISTRY membership itself — see supervisor.py's module docstring).

Follows tests/api/test_runtime_config_api.py's pattern of monkeypatching the
target module's own ``get_session`` reference to a fake context manager bound
to a fresh in-memory sqlite DB, rather than relying on db/session.py's
process-global cached engine (which would leak state across tests / not see
POSTGRES_URL changes made after first use).
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import CollectionRun, RuntimeConfig
from infra_brain.supervisor import dispatch

from tests.support.pg import make_engine


@pytest.fixture
def fake_db(monkeypatch):
    """Bind infra_brain.runtime_flags' get_session to a private in-memory DB.

    The override lookup itself moved out of supervisor.py into runtime_flags.py
    so graph.py's sweep-collector planning can share it (the toggle was a no-op
    under ``sweep_graph_enabled`` while it was supervisor-private), so that is
    the module whose ``get_session`` reference these tests rebind.
    """
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.runtime_flags as rf

    monkeypatch.setattr(rf, "get_session", _get_session)
    return eng


def _add_toggle(eng, domain: str, value: str) -> None:
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key=f"dispatchable__{domain}",
                value_type="bool",
                value=value,
                category="collector_toggle",
            )
        )
        s.commit()


def test_runtime_toggle_pauses_a_dispatchable_agent(fake_db):
    _add_toggle(fake_db, "linux", "false")

    mock_agent_instance = MagicMock()
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux", trigger_type="scheduled", scope="all")

    assert result.status == "skipped"
    assert result.domain == "linux"
    mock_agent_instance.run.assert_not_called()
    hook.assert_not_called()


def test_paused_dispatch_writes_a_real_collection_run_row(fake_db):
    """The skip path must PERSIST the skip, not just return an in-memory result.

    get_agent_roster()/the dashboard read last_run from collection_runs; a
    skip that writes nothing leaves a paused domain's roster entry frozen at
    its last real run, which reads as dormant/unwired rather than paused.
    """
    _add_toggle(fake_db, "linux", "false")

    mock_agent_instance = MagicMock()
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook"),
    ):
        result = dispatch(domain="linux", trigger_type="scheduled", scope="all")

    with Session(fake_db) as s:
        runs = s.query(CollectionRun).filter(CollectionRun.domain == "linux").all()
        assert len(runs) == 1
        run = runs[0]
        # The returned run_id must be the persisted row's id, not a fabricated
        # one — otherwise nothing downstream can correlate the two.
        assert run.id == result.run_id
        assert run.status == "skipped"
        assert run.resources_found == 0
        assert run.drift_count == 0
        assert run.trigger_type == "scheduled"
        assert run.finished_at is not None
        assert "dispatchable__linux" in (run.trigger_source or "")


def test_paused_dispatch_still_skips_when_the_run_row_write_fails(fake_db, monkeypatch):
    """Bookkeeping is best-effort: a failed row write must not raise or run the agent."""
    _add_toggle(fake_db, "linux", "false")
    monkeypatch.setattr(
        "infra_brain.supervisor.record_paused_run",
        lambda *a, **k: None,
    )

    mock_agent_instance = MagicMock()
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux")

    assert result.status == "skipped"
    assert result.run_id is not None
    mock_agent_instance.run.assert_not_called()
    hook.assert_not_called()


def test_unpaused_dispatch_writes_no_paused_run_row(fake_db):
    """A normal dispatch must not gain a spurious skipped row."""
    mock_result = MagicMock()
    mock_result.domain = "linux"
    mock_result.status = "completed"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook"),
    ):
        dispatch(domain="linux")

    with Session(fake_db) as s:
        assert s.query(CollectionRun).count() == 0


def test_paused_domains_bulk_read(fake_db):
    """paused_domains() returns only the domains whose override is falsey."""
    from infra_brain.runtime_flags import paused_domains

    _add_toggle(fake_db, "linux", "false")
    _add_toggle(fake_db, "dns", "true")
    _add_toggle(fake_db, "k8s", "off")

    assert paused_domains() == {"linux", "k8s"}


def test_paused_domains_fails_open(monkeypatch):
    """An unreachable DB must report nothing paused, not crash the caller."""
    from infra_brain.runtime_flags import paused_domains

    @contextmanager
    def _broken_get_session():
        raise RuntimeError("no such table: runtime_config")
        yield  # pragma: no cover - unreachable, keeps this a generator

    import infra_brain.runtime_flags as rf

    monkeypatch.setattr(rf, "get_session", _broken_get_session)
    assert paused_domains() == set()


def test_runtime_toggle_true_does_not_pause(fake_db):
    _add_toggle(fake_db, "linux", "true")

    mock_result = MagicMock()
    mock_result.domain = "linux"
    mock_result.status = "completed"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux", trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    mock_agent_instance.run.assert_called_once()
    hook.assert_called_once_with(mock_result)


def test_no_override_row_falls_through_to_normal_dispatch(fake_db):
    """Absence of a dispatchable__<domain> row must not change existing behavior."""
    mock_result = MagicMock()
    mock_result.domain = "linux"
    mock_result.status = "completed"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux")

    assert result.status == "completed"
    mock_agent_instance.run.assert_called_once()
    hook.assert_called_once_with(mock_result)


def test_override_lookup_failure_degrades_to_normal_dispatch(monkeypatch):
    """A broken/unmigrated table or unreachable DB must fail OPEN, not block dispatch."""

    @contextmanager
    def _broken_get_session():
        raise RuntimeError("no such table: runtime_config")
        yield  # pragma: no cover - unreachable, keeps this a generator

    import infra_brain.runtime_flags as rf

    monkeypatch.setattr(rf, "get_session", _broken_get_session)

    mock_result = MagicMock()
    mock_result.domain = "linux"
    mock_result.status = "completed"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux")

    assert result.status == "completed"
    mock_agent_instance.run.assert_called_once()
    hook.assert_called_once_with(mock_result)


def test_toggle_does_not_affect_other_domains(fake_db):
    """A dispatchable__linux=false row must not pause an unrelated domain."""
    _add_toggle(fake_db, "linux", "false")

    mock_result = MagicMock()
    mock_result.domain = "dns"
    mock_result.status = "completed"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockDnsAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"dns": MockDnsAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="dns")

    assert result.status == "completed"
    mock_agent_instance.run.assert_called_once()
    hook.assert_called_once_with(mock_result)
