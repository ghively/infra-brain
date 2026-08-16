"""Phase 2 Task 4: sweep_graph_enabled scheduler wiring.

Strictly opt-in — disabled (default) must register exactly today's per-domain
collector crons; enabled must register ONE sweep job and skip the per-domain
collector crons for sweep_members()[Tier.COLLECTOR] domains only (reporters/
on-demand/reasoner crons untouched).
"""

import sys
from unittest.mock import patch

from infra_brain.etl.spec import Tier, retired_domains, sweep_members
from infra_brain.scheduler import SchedulerService, _DEFAULT_SCHEDULES

_COLLECTOR_DOMAINS = set(sweep_members()[Tier.COLLECTOR])

# Retired domains (AgentSpec.retired) keep their cron string in
# _DEFAULT_SCHEDULES — it is the cadence they would resume on — but register no
# job in either sweep mode, so they are outside what these tests assert about.
# They also never appear in _COLLECTOR_DOMAINS: sweep_members() drops them.
_RETIRED_DOMAINS = retired_domains()
_SCHEDULED_DOMAINS = {d for d in _DEFAULT_SCHEDULES if d not in _RETIRED_DOMAINS}


def _job_ids(svc) -> set[str]:
    return {j.id for j in svc._scheduler.get_jobs()}


# ---------------------------------------------------------------------------
# Disabled (default): byte-identical to pre-Task-4 behavior.
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_sweep_disabled_registers_every_default_domain_job(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()
    try:
        ids = _job_ids(svc)
        assert "infra_brain_sweep_graph" not in ids
        for domain in _SCHEDULED_DOMAINS:
            assert f"infra_brain_{domain}" in ids
        for domain in _RETIRED_DOMAINS:
            assert f"infra_brain_{domain}" not in ids, f"retired {domain} was scheduled"
    finally:
        svc.stop()


def test_sweep_disabled_never_imports_graph_module():
    """Default (sweep_graph_enabled=False) must never import infra_brain.graph
    at scheduler startup — disabled deployments shouldn't pay langgraph's
    import cost or risk import-time side effects from the sweep module.

    Removing "infra_brain.graph" from sys.modules and never restoring it would
    corrupt module identity for the rest of the test session: any other test
    file holding a direct reference to the original module object (e.g.
    ``import infra_brain.graph as graph_mod`` at collection time) would keep
    using that stale object while a later re-import created a second,
    divergent module instance with its own copy of module-level globals
    (_PRODUCTION_CHECKPOINTER, _PRODUCTION_GRAPH, ...). Always restore the
    original entry afterward, whether or not the assertion holds.
    """
    original = sys.modules.pop("infra_brain.graph", None)
    try:
        with (
            patch("infra_brain.scheduler.get_settings") as mock_settings,
            patch("infra_brain.scheduler.dispatch"),
            patch("infra_brain.scheduler.try_acquire", return_value=True),
            patch("infra_brain.scheduler.release"),
        ):
            mock_settings.return_value.vsphere_full_inventory_enabled = True
            mock_settings.return_value.sweep_graph_enabled = False
            mock_settings.return_value.headless_runner_mcp_token = ""
            svc = SchedulerService()
            svc.start()
            try:
                assert "infra_brain.graph" not in sys.modules
            finally:
                svc.stop()
    finally:
        if original is not None:
            sys.modules["infra_brain.graph"] = original


# ---------------------------------------------------------------------------
# Enabled: one sweep job registered; sweep-member COLLECTOR crons skipped.
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_sweep_enabled_registers_single_sweep_job(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = True
    mock_settings.return_value.sweep_graph_schedule = "0 */4 * * *"
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()
    try:
        ids = _job_ids(svc)
        assert "infra_brain_sweep_graph" in ids
    finally:
        svc.stop()


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_sweep_enabled_skips_collector_tier_crons_only(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = True
    mock_settings.return_value.sweep_graph_schedule = "0 */4 * * *"
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()
    try:
        ids = _job_ids(svc)
        for domain in _COLLECTOR_DOMAINS:
            if domain in _DEFAULT_SCHEDULES:
                assert f"infra_brain_{domain}" not in ids, (
                    f"sweep-member COLLECTOR domain {domain} must not keep its own cron"
                )
        # Reporter/reasoner/on-demand crons (not sweep collector members) are untouched.
        for domain in _SCHEDULED_DOMAINS:
            if domain not in _COLLECTOR_DOMAINS:
                assert f"infra_brain_{domain}" in ids, (
                    f"non-collector domain {domain} lost its cron"
                )
        # query/health stays registered in both modes. The vsphere pulse used to
        # be asserted here too, but vsphere is retired — retirement outranks the
        # sweep mode, and tests/scheduler/test_scheduler.py::
        # test_retirement_dominates_the_full_inventory_flag owns that assertion.
        assert "infra_brain_vsphere_pulse" not in ids
        assert "infra_brain_query_health" in ids
        assert "infra_brain_collection_health" in ids
    finally:
        svc.stop()
