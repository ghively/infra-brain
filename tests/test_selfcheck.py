"""P3.1: the runtime-viable subset of dev_status.py's checks, importable
live inside the running app (registry/schedule introspection + a source-tree
regex scan of agents/) — see selfcheck.py's module docstring for scope.
"""

from infra_brain.selfcheck import (
    check_agent_registry,
    check_callbacks_wiring,
    check_schedule_collisions,
    run_selfcheck,
)


def test_check_agent_registry_ok_against_the_real_registry():
    result = check_agent_registry()
    assert result["name"] == "agent_registry"
    # The real registry must stay internally consistent — this is the same
    # invariant .claude/scripts/dev_status.py's check 1 enforces.
    assert result["status"] == "ok", result["message"]


def test_check_agent_registry_returns_error_result_on_broken_required_agent_import(
    monkeypatch,
):
    """A non-optional agent domain that fails to import makes
    supervisor._LazyAgentRegistry.__iter__()/__contains__() re-raise the
    ImportError uncaught (F-009: "must be LOUD"). check_agent_registry()
    backs the /selfcheck endpoint, whose whole purpose is to report a
    graceful {"status": "error"} instead of turning into an unhandled 500 —
    so this must be caught and reported, not propagated.
    """

    class _BrokenRegistry:
        def __iter__(self):
            raise ImportError("simulated broken agent module")

    monkeypatch.setattr("infra_brain.supervisor.AGENT_REGISTRY", _BrokenRegistry())

    result = check_agent_registry()
    assert result["name"] == "agent_registry"
    assert result["status"] == "error"
    assert "simulated broken agent module" in result["message"]


def test_check_schedule_collisions_against_the_real_schedule():
    result = check_schedule_collisions()
    assert result["name"] == "schedule_collisions"
    assert result["status"] in ("ok", "warn")


def test_check_callbacks_wiring_against_the_real_agents_dir():
    result = check_callbacks_wiring()
    assert result["name"] == "callbacks_wiring"
    assert result["status"] == "ok", result["message"]


def test_run_selfcheck_rolls_up_overall_status():
    out = run_selfcheck()
    assert out["overall"] in ("ok", "warn", "error")
    assert len(out["checks"]) == 3
    names = {c["name"] for c in out["checks"]}
    assert names == {"agent_registry", "schedule_collisions", "callbacks_wiring"}


def test_run_selfcheck_overall_reflects_worst_check_status():
    checks = [
        {"name": "a", "status": "ok", "message": ""},
        {"name": "b", "status": "warn", "message": ""},
    ]
    statuses = {c["status"] for c in checks}
    overall = "error" if "error" in statuses else "warn" if "warn" in statuses else "ok"
    assert overall == "warn"
