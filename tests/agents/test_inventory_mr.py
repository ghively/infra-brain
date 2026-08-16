import uuid
from unittest.mock import patch, MagicMock
from infra_brain.agents.inventory_mr import InventoryMRAgent


def _make_session_mock():
    """Return a mock get_session context manager (no real DB needed)."""
    mock_session = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, mock_session


def test_propose_inventory_fix_returns_mr_url():
    drift_id = uuid.uuid4()
    mock_ctx, _ = _make_session_mock()
    with (
        patch(
            "infra_brain.agents.inventory_mr.create_inventory_mr",
            return_value="https://gitlab.example.com/mr/42",
        ) as mock_mr,
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        url = agent.propose_inventory_fix(
            drift_event_id=drift_id,
            project_id=5,
            inventory_file_path="inventories/production/01-tn.yml",
            corrected_content="all:\n  hosts:\n    new-server:",
            description="Host new-server detected in Ansible facts but not in inventory",
        )
    assert url == "https://gitlab.example.com/mr/42"
    call_args = mock_mr.call_args
    assert call_args.kwargs["project_id"] == 5
    assert (
        "infra-brain" in call_args.kwargs["mr_title"].lower()
        or "drift" in call_args.kwargs["mr_title"].lower()
    )


def test_propose_inventory_fix_includes_drift_id_in_branch():
    drift_id = uuid.uuid4()
    mock_ctx, _ = _make_session_mock()
    with (
        patch(
            "infra_brain.agents.inventory_mr.create_inventory_mr",
            return_value="https://gitlab/mr/1",
        ) as mock_mr,
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        agent.propose_inventory_fix(drift_id, 5, "inv.yml", "content", "desc")
    branch_name = mock_mr.call_args.kwargs["branch_name"]
    assert str(drift_id)[:8] in branch_name


def test_inventory_mr_agent_collect_returns_empty_list():
    agent = InventoryMRAgent.__new__(InventoryMRAgent)
    assert agent.collect() == []


def test_propose_inventory_fix_passes_correct_kwargs():
    drift_id = uuid.uuid4()
    short_id = str(drift_id)[:8]
    mock_ctx, _ = _make_session_mock()
    with (
        patch(
            "infra_brain.agents.inventory_mr.create_inventory_mr",
            return_value="https://gitlab/mr/99",
        ) as mock_mr,
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        url = agent.propose_inventory_fix(
            drift_event_id=drift_id,
            project_id=7,
            inventory_file_path="inventories/staging/hosts.yml",
            corrected_content="all:\n  hosts:\n    staging-host:",
            description="Staging host detected",
        )
    assert url == "https://gitlab/mr/99"
    kwargs = mock_mr.call_args.kwargs
    assert kwargs["project_id"] == 7
    assert kwargs["file_path"] == "inventories/staging/hosts.yml"
    assert kwargs["new_content"] == "all:\n  hosts:\n    staging-host:"
    assert short_id in kwargs["branch_name"]
    assert short_id in kwargs["commit_message"]


# --- C1: Audit log tests ---


def test_propose_inventory_fix_writes_audit_log_on_success():
    """C1: every successful create_inventory_mr call must write an AgentActionLog row."""
    drift_id = uuid.uuid4()
    mr_url = "https://gitlab.example.com/mr/77"

    mock_session = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("infra_brain.agents.inventory_mr.create_inventory_mr", return_value=mr_url),
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        url = agent.propose_inventory_fix(
            drift_event_id=drift_id,
            project_id=5,
            inventory_file_path="inventories/production/01-tn.yml",
            corrected_content="all:\n  hosts:\n    new-server:",
            description="Host drift",
        )

    assert url == mr_url
    mock_session.add.assert_called_once()
    log_row = mock_session.add.call_args[0][0]
    assert log_row.tool == "create_inventory_mr"
    assert log_row.verdict == "allow"
    assert log_row.status == "ok"
    assert mr_url in (log_row.args_summary or "")
    mock_session.commit.assert_called_once()


def test_propose_inventory_fix_writes_audit_log_on_failure():
    """C1: on MR creation failure the audit log row must have status='error'."""
    drift_id = uuid.uuid4()

    mock_session = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "infra_brain.agents.inventory_mr.create_inventory_mr",
            side_effect=RuntimeError("gitlab error"),
        ),
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        try:
            agent.propose_inventory_fix(drift_id, 5, "inv.yml", "content", "desc")
        except RuntimeError:
            pass

    mock_session.add.assert_called_once()
    log_row = mock_session.add.call_args[0][0]
    assert log_row.status == "error"
    assert log_row.tool == "create_inventory_mr"
    mock_session.commit.assert_called_once()


def test_verdict_deny_recorded_on_write_gate_block():
    """F-004.3: AgentActionLog.verdict must reflect the real decision.

    Fails today: verdict is hardcoded "allow" on every path.
    """
    import uuid
    from unittest.mock import MagicMock, patch

    import pytest

    from infra_brain.agents.inventory_mr import InventoryMRAgent

    session = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "infra_brain.agents.inventory_mr.create_inventory_mr",
            side_effect=PermissionError("[write-gate] PAN detected"),
        ),
        patch("infra_brain.agents.inventory_mr.get_session", return_value=ctx),
    ):
        agent = InventoryMRAgent()
        with pytest.raises(RuntimeError):
            agent.propose_inventory_fix(
                drift_event_id=uuid.uuid4(),
                project_id=1,
                inventory_file_path="hosts.yml",
                corrected_content="x",
                description="d",
            )
    row = session.add.call_args.args[0]
    assert row.verdict == "deny"


# --- Registry wiring tests ---


def test_inventory_mr_agent_in_registry():
    """InventoryMRAgent must be discoverable via AGENT_REGISTRY under 'inventory_mr'."""
    from infra_brain.supervisor import AGENT_REGISTRY
    from infra_brain.agents.inventory_mr import InventoryMRAgent

    assert "inventory_mr" in AGENT_REGISTRY, (
        "'inventory_mr' not found in AGENT_REGISTRY — wiring step missing in supervisor.py"
    )
    assert AGENT_REGISTRY["inventory_mr"] is InventoryMRAgent


def test_propose_inventory_fix_chains_original_exception():
    """AA-R-17: the final RuntimeError raised on failure must chain the
    ORIGINAL exception via `from`, not discard it — so a debugger/traceback
    can see the real cause, not just a flattened string."""
    drift_id = uuid.uuid4()
    mock_ctx, _ = _make_session_mock()
    original = ValueError("upstream GitLab API broke")

    with (
        patch("infra_brain.agents.inventory_mr.create_inventory_mr", side_effect=original),
        patch("infra_brain.agents.inventory_mr.get_session", return_value=mock_ctx),
    ):
        agent = InventoryMRAgent.__new__(InventoryMRAgent)
        try:
            agent.propose_inventory_fix(drift_id, 5, "inv.yml", "content", "desc")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert exc.__cause__ is original


def test_inventory_mr_agent_in_skip_hook():
    """'inventory_mr' must be in SKIP_HOOK so dispatch() does not re-trigger the post-hook."""
    from infra_brain.supervisor import SKIP_HOOK

    assert "inventory_mr" in SKIP_HOOK, (
        "'inventory_mr' not found in SKIP_HOOK — dispatching it would trigger a spurious "
        "post-collection hook (drift/notification re-run)"
    )
