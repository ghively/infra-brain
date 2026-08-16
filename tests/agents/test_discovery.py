"""Tests for DiscoveryAgent — autonomous, LLM-driven infrastructure discovery."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.agents.base import CollectOutcome
from infra_brain.etl.base import CollectorSkipped
from infra_brain.agents.discovery import (
    _PROMPT,
    DiscoveryAgent,
    DiscoveryFinding,
    DiscoveryFindingList,
)

from tests.support.pg import make_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session(known_types: list[str] | None = None):
    """Return a minimal mock session whose Resource.type query returns known_types."""
    mock_session = MagicMock()
    rows = [(t,) for t in (known_types or [])]
    mock_session.query.return_value.distinct.return_value = rows
    return mock_session


@pytest.fixture(autouse=True)
def _discovery_has_a_fleet_source(monkeypatch):
    """Give DiscoveryAgent a live fleet source for every test in this module.

    collect() self-skips when BOTH vSphere and the Ansible inventory are
    unavailable — with neither, there is nothing to cross-reference against and
    the agent cannot do its job (see its collect() guard). These tests all
    exercise the reason()/finalization path BEYOND that guard, so they need a
    source configured. The guard itself is covered by its own tests below,
    which opt out of this fixture explicitly.
    """
    import infra_brain.agents.discovery as _disc

    real_init = _disc.DiscoveryAgent.__init__

    def _init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.settings.vsphere_hosts = "vcenter.test"

    monkeypatch.setattr(_disc.DiscoveryAgent, "__init__", _init)


def _patch_structured(findings: DiscoveryFindingList | list, *, side_effect=None):
    """Patch discovery.get_chat_model so with_structured_output(...).invoke
    returns *findings* (a DiscoveryFindingList) — mirrors RootCauseAgent's
    test helper (test_rootcause.py's ``_patch_structured``) for the same
    get_chat_model().with_structured_output(...) pattern."""
    model = MagicMock()
    if side_effect is not None:
        model.with_structured_output.return_value.invoke.side_effect = side_effect
    else:
        model.with_structured_output.return_value.invoke.return_value = findings
    return patch("infra_brain.agents.discovery.get_chat_model", return_value=model), model


# ---------------------------------------------------------------------------
# Test 1: reason() is called with read-only tools (no write tool)
# ---------------------------------------------------------------------------


def test_discovery_uses_reason_with_readonly_tools():
    """collect() must call reason() and must NOT include any write tool."""
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[]))
    with (
        patch.object(DiscoveryAgent, "reason", return_value="Nothing new found.") as mock_reason,
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        mock_session = _make_mock_session([])
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        agent.collect()

    assert mock_reason.called, "collect() must call self.reason()"

    # Extract tools from the call (positional arg or keyword)
    call_args = mock_reason.call_args
    tools_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("tools")
    assert tools_arg is not None, "reason() must be called with a tools list"

    tool_names = {t.name for t in tools_arg}
    assert "create_inventory_mr" not in tool_names, (
        "DiscoveryAgent must NOT include write tool 'create_inventory_mr' in its tool list"
    )


# ---------------------------------------------------------------------------
# Test 2: findings are returned as discovery_finding items
# ---------------------------------------------------------------------------


def test_discovery_records_findings_as_observations():
    """collect() must return items typed 'discovery_finding' from the
    structured-output finalization result."""
    finding = DiscoveryFinding(
        hostname="srv-x",
        discovered_type="unknown_host",
        evidence="in ansible, not in DB",
    )
    mock_session = _make_mock_session(["vsphere_vm", "k8s_pod"])
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[finding]))

    with (
        patch.object(DiscoveryAgent, "reason", return_value="Found srv-x in ansible facts."),
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        items = agent.collect()

    assert items, "collect() must return at least one item when findings are non-empty"
    assert items[0]["type"] == "discovery_finding"
    assert items[0]["name"] == "srv-x"
    assert items[0]["data"]["discovered_type"] == "unknown_host"
    assert "ansible" in items[0]["data"]["evidence"]


# ---------------------------------------------------------------------------
# Test 3: structured-output finalization failure → surfaces as a non-fatal
# CollectOutcome, NOT a silent empty-success (TRK-148 contract preserved).
# ---------------------------------------------------------------------------


def test_discovery_finalization_failure_yields_diagnostic_outcome():
    """If the structured-output finalization call raises (e.g. validation
    fails on every retry attempt), collect() must surface a diagnostic
    CollectOutcome (errors set, items empty) instead of a silent []."""
    mock_session = _make_mock_session([])
    patcher, _model = _patch_structured(
        None, side_effect=RuntimeError("model refused to produce structured output")
    )

    with (
        patch.object(
            DiscoveryAgent, "reason", return_value="I could not find anything relevant."
        ) as mock_reason,
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        result = agent.collect()

    assert isinstance(result, CollectOutcome), (
        f"Expected a CollectOutcome diagnosing the finalization failure, got {result!r}"
    )
    assert result.items == []
    assert result.errors, "expected a diagnostic error message for the finalization failure"
    assert "I could not find anything relevant." in result.errors[0]
    # Per the CollectOutcome R3 contract (errors present, no items/count_override
    # -> "failed"), this is NOT "ok" — the run must not finalize as "completed".
    assert result.status == "failed"
    # reason() itself is called exactly once — TRK-248 removed the old
    # parse-failure corrective retry (superseded: structured output no longer
    # fails to parse conversational text, so that retry trigger no longer
    # exists).
    assert mock_reason.call_count == 1


# ---------------------------------------------------------------------------
# TRK-248 (parse-failure fix verification): the live intermittent failure
# (collection_runs 2026-07-06 / 07-13 / 07-27) was the model ignoring the
# discovery task and answering with a conversational capability-listing
# greeting instead of findings JSON. Under the OLD free-text-JSON-parsing
# implementation, this raw text would fail `_parse_findings()` (no JSON
# substring present at all) and — after one retry — surface as a diagnostic
# CollectOutcome failure. Under the NEW structured-output implementation,
# that same conversational text is handed to the schema-constrained
# finalization call, which is not JSON-regex-based and so does not fail to
# parse it — it correctly extracts "no findings" (or, if the analysis
# happens to actually contain identifiable findings buried in prose, extracts
# those). This test proves the specific parse-failure mode is fixed: text
# that has NO parseable JSON substring anywhere in it (so the old
# `_parse_findings` would return None) now flows cleanly through
# `_finalize_findings` to a normal, completed result.
# ---------------------------------------------------------------------------

_CONVERSATIONAL_GREETING = (
    "I am ready to assist you! I have access to a comprehensive set of tools "
    "covering various domains, including:\n"
    "* **DevOps & Source Code Management:** Interacting with GitLab...\n"
    "* **Infrastructure Management:** Checking VMware vSphere...\n"
    "Please tell me what you would like to do. For example, you could ask me to:\n"
    '* "List all accessible GitLab projects."\n'
    '* "Check the power state of my vSphere VMs."\n'
)


def test_discovery_conversational_output_no_longer_causes_parse_failure():
    """The exact TRK-153 failure text (which has no JSON substring at all,
    so the old regex-based `_parse_findings` returned None and the run
    failed) must NOT cause a failure under the new structured-output path —
    the finalization call is schema-constrained, not text-parsed, so it
    reports a legitimate "found nothing" completion instead."""
    mock_session = _make_mock_session([])
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[]))

    with (
        patch.object(
            DiscoveryAgent, "reason", return_value=_CONVERSATIONAL_GREETING
        ) as mock_reason,
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        result = agent.collect()

    # No exception, no CollectOutcome failure — a plain (empty) items list,
    # exactly like a genuine "looked and found nothing new" run.
    assert result == [], f"Expected a clean empty result, got {result!r}"
    assert mock_reason.call_count == 1


def test_discovery_conversational_analysis_can_still_yield_real_findings():
    """Even when reason()'s output is conversational/free-form (not literal
    JSON), the finalization call can still extract genuine findings from it
    — proving extraction no longer depends on the model emitting a JSON blob
    as its final message."""
    mock_session = _make_mock_session([])
    finding = DiscoveryFinding(hostname="srv-r", discovered_type="unknown_host", evidence="octopus")
    patcher, model = _patch_structured(DiscoveryFindingList(findings=[finding]))

    with (
        patch.object(
            DiscoveryAgent,
            "reason",
            return_value=(
                "Sure! While looking through Octopus machines, I noticed srv-r is "
                "present there but has no corresponding inventory row."
            ),
        ),
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        items = agent.collect()

    assert isinstance(items, list)
    assert items[0]["type"] == "discovery_finding"
    assert items[0]["name"] == "srv-r"
    # The finalization call must receive the analysis text (not re-derive it).
    finalize_prompt = model.with_structured_output.return_value.invoke.call_args.args[0]
    assert "srv-r" in finalize_prompt


def test_discovery_finalization_flows_through_callbacks():
    """CLAUDE.md architecture constraint #1: get_chat_model() must always be
    called with callbacks= — never bare."""
    mock_session = _make_mock_session([])
    sentinel_callbacks = ["sentinel-callback"]
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[]))

    with (
        patch.object(DiscoveryAgent, "reason", return_value="Nothing new."),
        patch("infra_brain.agents.discovery.get_session") as gs,
        patch("infra_brain.agents.discovery.get_chat_model") as gcm,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False
        model = MagicMock()
        model.with_structured_output.return_value.invoke.return_value = DiscoveryFindingList(
            findings=[]
        )
        gcm.return_value = model

        agent = DiscoveryAgent()
        agent.callbacks = sentinel_callbacks
        agent.collect()

    gcm.assert_called_once()
    assert gcm.call_args.kwargs.get("callbacks") == sentinel_callbacks
    assert gcm.call_args.kwargs.get("role") == "discovery"


def test_discovery_prompt_forbids_conversational_output():
    """Prevention side of the discovery prompt: the model is told this is a
    non-interactive automated task."""

    rendered = _PROMPT.format(known_types=["vsphere_vm"])
    assert "non-interactive" in rendered
    assert "Do NOT introduce yourself" in rendered


# ---------------------------------------------------------------------------
# Test 4: DB failure → returns [] without crashing
# ---------------------------------------------------------------------------


def test_discovery_resilient_on_db_error():
    """If get_session() raises, collect() must return [] and not propagate."""
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[]))
    with (
        patch.object(DiscoveryAgent, "reason", return_value="Nothing new."),
        patch("infra_brain.agents.discovery.get_session", side_effect=RuntimeError("db down")),
        patcher,
    ):
        agent = DiscoveryAgent()
        result = agent.collect()

    assert result == [], f"Expected [] on DB error, got {result!r}"


# ---------------------------------------------------------------------------
# Test 5: reason() raises → collect() propagates
# ---------------------------------------------------------------------------


def test_discovery_resilient_on_reason_error():
    """If reason() raises, collect() must propagate (F-007: status='failed'),
    not silently return [] as a fake-successful empty run."""
    mock_session = _make_mock_session([])

    with (
        patch.object(DiscoveryAgent, "reason", side_effect=RuntimeError("LLM timeout")),
        patch("infra_brain.agents.discovery.get_session") as gs,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        with pytest.raises(RuntimeError, match="LLM timeout"):
            agent.collect()


# ---------------------------------------------------------------------------
# Test 6: reason() called with max_iters=10
# ---------------------------------------------------------------------------


def test_discovery_passes_max_iters_10():
    """collect() must call reason() with max_iters=10."""
    mock_session = _make_mock_session([])
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[]))

    with (
        patch.object(DiscoveryAgent, "reason", return_value="Nothing new.") as mock_reason,
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        agent.collect()

    call_args = mock_reason.call_args
    max_iters = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("max_iters")
    assert max_iters == 10, f"reason() must be called with max_iters=10, got {max_iters!r}"


# ---------------------------------------------------------------------------
# Test 7: domain is "discovery"
# ---------------------------------------------------------------------------


def test_discovery_agent_domain():
    """DiscoveryAgent.domain must be 'discovery'."""
    assert DiscoveryAgent.domain == "discovery"


def test_discovery_uses_sql_like_role():
    """DiscoveryAgent must declare the 'discovery' model role for Bedrock selection."""
    assert DiscoveryAgent.llm_role == "discovery"


# ---------------------------------------------------------------------------
# DiscoveryFinding / DiscoveryFindingList — structured-output schema (TRK-248)
# ---------------------------------------------------------------------------


def test_discovery_finding_requires_nonempty_hostname():
    with pytest.raises(Exception):
        DiscoveryFinding(hostname="   ")


def test_discovery_finding_list_defaults_to_empty():
    assert DiscoveryFindingList().findings == []


# ---------------------------------------------------------------------------
# invoke_structured_with_retry: transient validation failure is retried once
# before finalization succeeds (bounded retry, mirrors rootcause.py TRK-119).
# ---------------------------------------------------------------------------


def test_discovery_finalization_retries_transient_validation_failure():
    from pydantic import ValidationError as PydanticValidationError

    mock_session = _make_mock_session([])
    good = DiscoveryFindingList(
        findings=[DiscoveryFinding(hostname="srv-z", evidence="rapid7")]
    )

    # Build a real ValidationError instance to exercise the retryable path.
    try:
        DiscoveryFinding(hostname="")
        bad_validation_error = None
    except PydanticValidationError as e:
        bad_validation_error = e

    patcher, model = _patch_structured(
        None, side_effect=[bad_validation_error, good]
    )

    with (
        patch.object(DiscoveryAgent, "reason", return_value="Found srv-z via rapid7 scan."),
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        items = agent.collect()

    assert model.with_structured_output.return_value.invoke.call_count == 2
    assert items[0]["name"] == "srv-z"


# ---------------------------------------------------------------------------
# AA-R-16: run_id must be the REAL run_id set by ETLConnector.run(), not one
# inferred by querying the DB for the "newest in_progress" row for this
# domain (fragile under overlapping runs).
# ---------------------------------------------------------------------------


def test_run_checkpoints_against_the_real_run_id():
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    from infra_brain.db.models import CollectionRun, Snapshot

    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    finding = DiscoveryFinding(hostname="srv-x", discovered_type="unknown_host", evidence="e")
    patcher, _model = _patch_structured(DiscoveryFindingList(findings=[finding]))

    with (
        patch.object(DiscoveryAgent, "reason", return_value="Found srv-x."),
        patch("infra_brain.agents.discovery.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patcher,
    ):
        agent = DiscoveryAgent()
        result = agent.run(trigger_type="test", scope="all")

    assert result.status == "completed"

    with Session(eng) as v:
        runs = v.query(CollectionRun).filter_by(domain="discovery").all()
        assert len(runs) == 1
        real_run_id = runs[0].id

        # run() also writes an ordinary (resource_id-bound) Snapshot for the
        # discovery_finding Resource item collect() returns — filter to just
        # the run-scoped checkpoint row (resource_id IS NULL).
        checkpoints = v.query(Snapshot).filter(Snapshot.resource_id.is_(None)).all()
        # Every checkpoint Snapshot row must be linked to the ACTUAL
        # CollectionRun row created by this run() call.
        assert checkpoints, "expected at least one checkpoint Snapshot row"
        for row in checkpoints:
            assert row.run_id == real_run_id
            assert row.snapshot["_checkpoint"] is True


def test_gitlab_runners_tool_is_wired_into_discovery_tools():
    from infra_brain.agents.discovery import _DISCOVERY_TOOLS
    from infra_brain.tools.gitlab import gitlab_runners_tool

    assert gitlab_runners_tool in _DISCOVERY_TOOLS


# ---------------------------------------------------------------------------
# TRK-299 (GitLab #138): the model asked a human for a GitLab Project ID
# instead of chaining gitlab_projects_tool -> gitlab_repository_tree_tool,
# burning the full run before failing. Root cause: the prompt never told the
# model where project_id comes from. These tests cover both parts of the fix.
# ---------------------------------------------------------------------------


def test_discovery_prompt_teaches_project_id_discovery():
    """_PROMPT must explicitly teach the model to source project_id from
    gitlab_projects_tool's output instead of asking a human for it."""
    assert "gitlab_projects_tool" in _PROMPT
    assert "project_id" in _PROMPT
    lowered = _PROMPT.lower()
    assert "never ask a human" in lowered or "there is no human" in lowered


def test_discovery_clarifying_question_analysis_fails_run():
    """If reason()'s final analysis reads as a clarifying question deferring
    to a human (e.g. asking for a GitLab Project ID) rather than a real
    analysis, collect() must surface a diagnostic CollectOutcome instead of
    silently finalizing as a false '0 findings, completed' run."""
    mock_session = _make_mock_session([])
    clarifying_text = (
        "I can help with that, but could you provide the GitLab Project ID "
        "you'd like me to inspect?"
    )

    with (
        patch.object(DiscoveryAgent, "reason", return_value=clarifying_text) as mock_reason,
        patch("infra_brain.agents.discovery.get_session") as gs,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        result = agent.collect()

    assert isinstance(result, CollectOutcome), (
        f"Expected a CollectOutcome diagnosing the clarifying-question response, got {result!r}"
    )
    assert result.items == []
    assert result.errors, "expected a diagnostic error message"
    assert "clarifying question" in result.errors[0]
    assert result.status == "failed"
    assert mock_reason.call_count == 1


def test_discovery_conversational_greeting_still_not_misclassified_as_clarifying():
    """Guard against over-broad clarifying-question detection: the exact
    TRK-153 capability-listing greeting (already proven safe by
    test_discovery_conversational_output_no_longer_causes_parse_failure) must
    still NOT trip the new clarifying-question check."""
    from infra_brain.agents.discovery import _looks_like_clarifying_question

    assert not _looks_like_clarifying_question(_CONVERSATIONAL_GREETING)


# ---------------------------------------------------------------------------
# GitLab #159 — DiscoveryAgent's reason() must self-terminate before the
# collector's external timeout guard kills it.
#
# max_iters=10 * llm_request_timeout_seconds(120) * REASON_MAX_ATTEMPTS(3) =
# 3600s worst case, versus a 300s collect_timeout_seconds. Nothing bounded the
# reasoning loop, so ETLConnector._call_with_timeout killed the run mid-flight
# and recorded no reason — the mysterious 0-resource failed run.
# ---------------------------------------------------------------------------


def _discovery_agent():
    agent = DiscoveryAgent.__new__(DiscoveryAgent)
    agent.callbacks = []
    # A configured fleet source, so collect() gets past its no-source self-skip
    # guard. This helper bypasses __init__, so the module's autouse fixture
    # cannot reach it. settings was None before, which only worked because
    # nothing read it this early.
    agent.settings = SimpleNamespace(vsphere_hosts="vcenter.test", ansible_inventory_path="")
    return agent


def test_reason_deadline_is_under_the_collect_timeout_guard():
    """The budget must be strictly SMALLER than the guard that would kill it.

    Equal-or-larger is the bug: reason() would still lose the race and be
    killed externally with no diagnostic signal.
    """
    agent = _discovery_agent()
    settings = SimpleNamespace(collect_timeout_seconds=300)
    with patch("infra_brain.agents.discovery.get_settings", return_value=settings):
        deadline = agent._reason_deadline_seconds()
    assert deadline is not None
    assert deadline < 300, "reason() must self-terminate before _call_with_timeout fires"
    assert deadline > 0


def test_reason_deadline_honours_the_agentspec_per_domain_override():
    """It must resolve from the SAME source _call_with_timeout uses.

    If the spec override raised the collect budget but the deadline kept using
    the global default, reason() would be cut short well before the guard —
    the opposite failure, but still wrong.
    """
    agent = _discovery_agent()
    agent_type = type(agent)
    settings = SimpleNamespace(collect_timeout_seconds=300)
    with (
        patch.object(agent_type, "spec", SimpleNamespace(collect_timeout_seconds=900)),
        patch("infra_brain.agents.discovery.get_settings", return_value=settings),
    ):
        deadline = agent._reason_deadline_seconds()
    assert 300 < deadline < 900


def test_reason_deadline_is_none_when_collect_timeout_disabled():
    """No outer guard => no inner budget; don't invent a ceiling nobody asked for."""
    agent = _discovery_agent()
    settings = SimpleNamespace(collect_timeout_seconds=0)
    with (
        patch.object(type(agent), "spec", SimpleNamespace(collect_timeout_seconds=None)),
        patch("infra_brain.agents.discovery.get_settings", return_value=settings),
    ):
        assert agent._reason_deadline_seconds() is None


def test_collect_passes_the_deadline_through_to_reason():
    """Wiring guard: computing a deadline is useless if collect() drops it."""
    agent = _discovery_agent()
    agent._active_run_id = None
    agent._current_run_id = None
    with (
        patch.object(agent, "_reason_deadline_seconds", return_value=240.0),
        patch.object(agent, "reason", return_value="no new resource types found") as mock_reason,
        patch("infra_brain.agents.discovery.get_session", side_effect=Exception("no db")),
    ):
        agent.collect(scope="all")
    assert mock_reason.call_args.kwargs["deadline_seconds"] == 240.0


def test_discovery_names_iteration_budget_exhaustion_distinctly():
    """A run that ran out of reasoning steps must SAY so, not blame the parser.

    When reason() hits the recursion limit it recovers the last message, which
    is mid-exploration text rather than a final answer — so structured-output
    validation was always going to fail on it. Reporting that as
    "Invalid JSON: expected value at line 1 column 1" sent a reader hunting a
    parser bug instead of the real condition: the model never stopped exploring
    long enough to answer. Observed live on every discovery run.
    """
    mock_session = _make_mock_session([])
    patcher, _model = _patch_structured(
        None, side_effect=RuntimeError("Invalid JSON: expected value at line 1 column 1")
    )

    def _reason_hitting_limit(*_a, **_kw):
        # Mirror what llm_base does on GraphRecursionError.
        agent._last_reason_hit_iteration_limit = True
        return "Let me check the repository tree for that project..."

    with (
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        with patch.object(DiscoveryAgent, "reason", side_effect=_reason_hitting_limit):
            result = agent.collect()

    assert result.status == "failed"
    joined = " ".join(result.errors)
    assert "exhausted its reasoning-step budget" in joined
    assert "mid-exploration" in joined


def test_discovery_keeps_generic_message_when_budget_was_not_the_cause():
    """A finalization failure with the budget intact keeps the generic wording."""
    mock_session = _make_mock_session([])
    patcher, _model = _patch_structured(None, side_effect=RuntimeError("model refused"))

    with (
        patch.object(DiscoveryAgent, "reason", return_value="a complete-looking answer"),
        patch("infra_brain.agents.discovery.get_session") as gs,
        patcher,
    ):
        gs.return_value.__enter__ = lambda s: mock_session
        gs.return_value.__exit__ = lambda *a: False

        agent = DiscoveryAgent()
        agent._last_reason_hit_iteration_limit = False
        result = agent.collect()

    joined = " ".join(result.errors)
    assert "structured-output finalization failed" in joined
    assert "reasoning-step budget" not in joined


def test_discovery_skips_when_no_live_fleet_source(monkeypatch):
    """No vSphere and no Ansible inventory => self-skip, not a 300s burn.

    This agent's method is cross-referencing live infrastructure against the
    DB; _PROMPT's own two examples are Ansible facts and vSphere VMs. With both
    unavailable it is left with GitLab, which describes code rather than
    running hosts, so there is nothing it can discover. Live behaviour before
    this guard: every scheduled run spent its full 300s budget wandering GitLab
    and was recorded "reaped: exceeded max runtime", escalating 25x in a row.
    Three prompt strategies were measured against the real model and none
    produced a conclusion, because the task cannot be completed from GitLab
    alone.
    """
    agent = DiscoveryAgent.__new__(DiscoveryAgent)
    agent.callbacks = []
    agent.settings = SimpleNamespace(vsphere_hosts="", vsphere_host="", ansible_inventory_path="")

    with patch.object(agent, "reason") as mock_reason:
        with pytest.raises(CollectorSkipped, match="no live fleet source"):
            agent.collect(scope="all")
    # It must not burn a model call before deciding it has nothing to do.
    mock_reason.assert_not_called()


def test_discovery_runs_when_only_vsphere_is_configured():
    """Either source alone is enough — the guard requires BOTH to be missing."""
    agent = DiscoveryAgent.__new__(DiscoveryAgent)
    agent.callbacks = []
    agent.settings = SimpleNamespace(vsphere_hosts="vcenter.test", ansible_inventory_path="")
    agent._active_run_id = None
    agent._current_run_id = None

    with (
        patch.object(agent, "_reason_deadline_seconds", return_value=240.0),
        patch.object(agent, "reason", return_value="no new resource types found"),
        patch("infra_brain.agents.discovery.get_session", side_effect=Exception("no db")),
    ):
        agent.collect(scope="all")  # no CollectorSkipped


def test_discovery_runs_when_only_the_ansible_inventory_exists(tmp_path):
    """The inventory path must EXIST, not merely be set."""
    agent = DiscoveryAgent.__new__(DiscoveryAgent)
    agent.callbacks = []
    agent._active_run_id = None
    agent._current_run_id = None

    # Set but absent -> still skips.
    agent.settings = SimpleNamespace(
        vsphere_hosts="", vsphere_host="", ansible_inventory_path=str(tmp_path / "nope")
    )
    with pytest.raises(CollectorSkipped):
        agent.collect(scope="all")

    # Set and present -> runs.
    real = tmp_path / "inventory.yml"
    real.write_text("all:\n  hosts:\n")
    agent.settings = SimpleNamespace(
        vsphere_hosts="", vsphere_host="", ansible_inventory_path=str(real)
    )
    with (
        patch.object(agent, "_reason_deadline_seconds", return_value=240.0),
        patch.object(agent, "reason", return_value="no new resource types found"),
        patch("infra_brain.agents.discovery.get_session", side_effect=Exception("no db")),
    ):
        agent.collect(scope="all")
