import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import CollectionResult
from infra_brain.agents.windows import WindowsAgent
from infra_brain.db.models import CollectionRun, Resource, WindowsPatchState


def _session_cm(engine):
    @contextmanager
    def _cm():
        with Session(engine) as s:
            yield s

    return _cm


def _make_agent():
    agent = WindowsAgent.__new__(WindowsAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    # F-021: provide a non-empty password so tests don't hit CollectorSkipped.
    agent.settings.ansible_win_password = "testwinpass"
    agent.settings.ansible_win_user = "Administrator"
    agent.callbacks = []
    return agent


def test_collect_returns_windows_hosts():
    agent = _make_agent()
    ansible_output = {
        "win01": {
            "ansible_facts": {
                "ansible_os_name": "Windows Server 2022",
                "ansible_os_version": "10.0.20348",
                "ansible_architecture": "x86_64",
                "ansible_hostname": "win01",
                "ansible_domain": "corp.local",
                "ansible_services": {},
                "ansible_pending_updates": [],
            }
        }
    }
    with patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn:
        mock_fn.return_value = ansible_output
        result = agent.collect()
    assert len(result) == 1
    assert result[0]["type"] == "windows_host"
    assert result[0]["data"]["os_name"] == "Windows Server 2022"


def test_collect_returns_empty_on_no_hosts():
    agent = _make_agent()
    with patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn:
        mock_fn.return_value = {}
        result = agent.collect()
    assert result == []


def test_collect_raises_on_ansible_exception():
    """run_windows_ansible_facts failure propagates — BaseAgent.run() catches it and
    records status='failed', not status='completed'/resources_found=0."""
    agent = _make_agent()
    with patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn:
        mock_fn.side_effect = RuntimeError("ansible unreachable")
        with pytest.raises(RuntimeError, match="ansible unreachable"):
            agent.collect()


# --- windows_patch_state detail-table writes --------------------------------

_PATCH_FACTS = {
    "win01": {
        "ansible_facts": {
            "ansible_os_name": "Windows Server 2022",
            "ansible_hostname": "win01",
            "ansible_pending_updates": [
                {"kb": "KB5034123", "title": "Cumulative Update"},
                "KB5034441",
            ],
        }
    }
}


def test_write_windows_details_populates_patch_state(sqlite_engine, session_patcher):
    agent = _make_agent()
    agent._last_raw = _PATCH_FACTS
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="windows", type="windows_host", name="win01", source="WindowsAgent"))
        s.commit()

    # MR-J item 4: get_winrm_client now actually builds a client when
    # pywinrm + credentials are available. This test predates that fix and
    # exercises the Ansible-facts-only fallback path, so WinRM stays a no-op
    # here (a real network attempt to "win01" would hang/slow the suite) —
    # the WinRM collector path itself has its own dedicated tests below.
    with (
        session_patcher("infra_brain.agents.windows"),
        patch("infra_brain.agents.windows.get_winrm_client", return_value=None),
    ):
        agent._write_windows_details("all")

    with Session(sqlite_engine) as v:
        ps = v.query(WindowsPatchState).one()
        assert ps.hostname == "win01"
        assert ps.pending_count == 2
        assert ps.kb_list == ["KB5034123", "KB5034441"]
        assert ps.winrm_status == "reachable"
        assert ps.resource_id is not None
        assert ps.last_patched is None


def test_write_windows_details_idempotent(sqlite_engine, session_patcher):
    agent = _make_agent()
    agent._last_raw = _PATCH_FACTS
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="windows", type="windows_host", name="win01", source="WindowsAgent"))
        s.commit()

    with (
        session_patcher("infra_brain.agents.windows"),
        patch("infra_brain.agents.windows.get_winrm_client", return_value=None),
    ):
        agent._write_windows_details("all")
        agent._write_windows_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(WindowsPatchState).count() == 1


def test_write_windows_details_no_resource_skipped(sqlite_engine, session_patcher):
    """No matching windows Resource → row skipped, no orphan written."""
    agent = _make_agent()
    agent._last_raw = _PATCH_FACTS
    with (
        session_patcher("infra_brain.agents.windows"),
        patch("infra_brain.agents.windows.get_winrm_client", return_value=None),
    ):
        agent._write_windows_details("all")
    with Session(sqlite_engine) as v:
        assert v.query(WindowsPatchState).count() == 0


def test_write_details_surfaces_windows_failure(sqlite_engine):
    agent = _make_agent()
    result = CollectionResult(
        run_id=uuid.uuid4(), domain="windows", resources_found=0, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("patch-state write exploded")

    with patch("infra_brain.etl.base.get_session", _session_cm(sqlite_engine)):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("patch-state write exploded" in e for e in result.errors)


# --- ansible failure propagation tests (Commit 2 regression) ----------------


def test_windows_run_marks_failed_on_ansible_tool_error(engine):
    """run_windows_ansible_facts raising RuntimeError propagates through collect() so
    BaseAgent.run() records status='failed' with a populated error_message.
    Previously the exception was swallowed and the run was marked completed/0."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.windows.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn,
    ):
        mock_fn.side_effect = RuntimeError("ansible -m win_setup failed: unreachable")
        agent = WindowsAgent()
        # F-021: inject a non-empty password so collect() passes the credential guard.
        agent.settings.ansible_win_password = "testwinpass"
        agent.settings.ansible_win_user = "Administrator"
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "failed", "Run should be failed when ansible errors"
    assert result.resources_found == 0
    assert any("unreachable" in e for e in result.errors)

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run.status == "failed"
        assert run.error_message is not None
        assert "unreachable" in run.error_message


def test_windows_run_sets_drift_count_for_stopped_service(engine):
    """AA-C-2: WindowsAgent.run() must report a nonzero drift_count when the
    detail-write phase emits a service_stopped DriftEvent, and that event must
    carry collection_run_id so it is actually counted."""
    from infra_brain.db.models import DriftEvent, WindowsService

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with Session(engine) as s:
        resource = Resource(
            domain="windows", type="windows_host", name="drift-win-01", source="WindowsAgent"
        )
        s.add(resource)
        s.commit()
        s.add(
            WindowsService(
                resource_id=resource.id, name="MyService", state="Running", start_type="auto"
            )
        )
        s.commit()

    facts_with_stopped_service = {
        "drift-win-01": {
            "ansible_facts": {
                "ansible_os_name": "Windows Server 2022",
                "ansible_hostname": "drift-win-01",
                "ansible_services": {"MyService": {"state": "stopped", "start_mode": "auto"}},
                "ansible_pending_updates": [],
            }
        }
    }

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.windows.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn,
        # MR-J item 4: get_winrm_client now actually builds a client — keep
        # this test on the Ansible-facts-only path (a real network attempt to
        # "drift-win-01" would hang the suite); WinRM collectors are tested
        # separately.
        patch("infra_brain.agents.windows.get_winrm_client", return_value=None),
    ):
        mock_fn.return_value = facts_with_stopped_service
        agent = WindowsAgent()
        agent.settings.ansible_win_password = "testwinpass"
        agent.settings.ansible_win_user = "Administrator"
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.drift_count == 1, "service_stopped drift must be counted, not stuck at 0"

    with Session(engine) as v:
        event = v.query(DriftEvent).filter_by(drift_type="service_stopped").one()
        assert event.collection_run_id == result.run_id


def test_windows_run_completed_on_empty_inventory(engine):
    """run_windows_ansible_facts returning {} (empty inventory) is NOT an error —
    run should be status='completed' with resources_found=0."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.windows.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn,
    ):
        mock_fn.return_value = {}
        agent = WindowsAgent()
        # F-021: inject a non-empty password so collect() passes the credential guard.
        agent.settings.ansible_win_password = "testwinpass"
        agent.settings.ansible_win_user = "Administrator"
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.resources_found == 0
    assert result.errors == []
