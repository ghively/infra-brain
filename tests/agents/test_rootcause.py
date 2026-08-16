"""Tests for RootCauseAgent correlation + fleet digest."""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    AgentDecisionLog,
    CollectionRun,
    DriftEvent,
    EolRegistry,
    Resource,
    RootCauseNote,
    VulnQueueItem,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@contextmanager
def _session_factory(engine):
    with Session(engine) as s:
        yield s


def _make_agent():
    from infra_brain.agents.rootcause import RootCauseAgent

    agent = RootCauseAgent.__new__(RootCauseAgent)
    agent.settings = None
    agent.callbacks = []
    return agent


def test_correlates_drift_with_recent_deploy(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            CollectionRun(
                domain="octopus",
                trigger_type="webhook",
                started_at=now - timedelta(hours=1),
                status="completed",
            )
        )
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="version",
                status="open",
                detected_at=now,
            )
        )
        s.commit()

    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        _make_agent().collect()
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
    assert "octopus" in note.explanation
    assert note.correlated["candidates"]


def test_no_correlation_when_no_deploy(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-02", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="pkg",
                status="open",
                detected_at=now,
            )
        )
        s.commit()

    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        _make_agent().collect()
        _make_agent().collect()  # idempotent
    with Session(engine) as s:
        notes = s.query(RootCauseNote).all()
    assert len(notes) == 1
    assert "manual or external" in notes[0].explanation


def test_correlate_recent_deploys_pure_function(engine):
    """Phase 3 Task 1: correlate_recent_deploys extracted as a pure, module-level
    helper — same reasoning as collect(), callable directly against pre-fetched
    candidates without touching the DB for CollectionRun."""
    from infra_brain.agents.rootcause import correlate_recent_deploys

    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-04", source="LinuxAgent")
        s.add(r)
        s.flush()
        de = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="version",
            status="open",
            detected_at=now,
        )
        s.add(de)
        s.flush()

        run = CollectionRun(
            domain="octopus",
            trigger_type="webhook",
            started_at=now - timedelta(hours=1),
            status="completed",
        )
        s.add(run)
        s.flush()

        result = correlate_recent_deploys(s, de, [run], window_hours=6)
        assert result.top is run
        assert result.candidates == [run]
        assert "octopus" in result.explanation
        assert result.correlated["candidates"]


def test_correlate_recent_deploys_no_candidates(engine):
    """No candidates in the window -> empty CorrelationResult with the
    'manual or external' explanation and top=None."""
    from infra_brain.agents.rootcause import correlate_recent_deploys

    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-05", source="LinuxAgent")
        s.add(r)
        s.flush()
        de = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="pkg",
            status="open",
            detected_at=now,
        )
        s.add(de)
        s.flush()

        # A run far outside the 6h window is excluded by the helper's own filter.
        stale_run = CollectionRun(
            domain="octopus",
            trigger_type="scheduled",
            started_at=now - timedelta(hours=12),
            status="completed",
        )
        s.add(stale_run)
        s.flush()

        result = correlate_recent_deploys(s, de, [stale_run], window_hours=6)
        assert result.top is None
        assert result.candidates == []
        assert "manual or external" in result.explanation
        assert result.correlated["candidates"] == []


def test_rootcause_domain():
    from infra_brain.agents.rootcause import RootCauseAgent

    assert RootCauseAgent.domain == "rootcause"


def test_no_notes_when_no_open_drift(engine):
    """No open drift events → no notes written, collect() returns items=[]."""
    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        result = _make_agent().collect()
    assert result.items == []
    assert result.count_override == 0
    with Session(engine) as s:
        assert s.query(RootCauseNote).count() == 0


def test_digest_markdown_counts(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-03", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(DriftEvent(resource_id=r.id, drift_type="config_drift", field="x", status="open"))
        s.add(VulnQueueItem(resource_id=r.id, cve_id="CVE-Z", severity="critical", status="open"))
        s.add(EolRegistry(resource_id=r.id, asset_name="old", eol_date=now - timedelta(days=5)))
        s.commit()

    from infra_brain import digest

    with patch.object(digest, "get_session", lambda: _session_factory(engine)):
        md = digest.build_digest_markdown(now)
    assert "Fleet Digest" in md
    assert "Open drift events | 1" in md
    assert "critical: 1" in md
    assert "EOL assets overdue | 1" in md


# ---------------------------------------------------------------------------
# Phase 3 Task 2: RootCause LLM reasoner
# ---------------------------------------------------------------------------


def _make_llm_agent(cap: int = 20):
    from infra_brain.agents.rootcause import RootCauseAgent

    agent = RootCauseAgent.__new__(RootCauseAgent)
    agent.settings = MagicMock(rootcause_llm_enabled=True, rootcause_llm_max_events_per_run=cap)
    agent.callbacks = []
    agent._llm = None
    return agent


def _seed_event(engine, *, trigger_source=None, pipeline_resource=False, name="web-10"):
    """Seed one open drift event + a deploy run 1h before detection.

    Returns (drift_event_id, resource_id).
    """
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name=name, source="LinuxAgent")
        s.add(r)
        s.flush()
        if pipeline_resource and trigger_source:
            s.add(
                Resource(
                    domain="octopus", type="project", name=trigger_source, source="OctopusAgent"
                )
            )
        s.add(
            CollectionRun(
                domain="octopus",
                trigger_type="webhook",
                trigger_source=trigger_source,
                started_at=now - timedelta(hours=1),
                status="completed",
            )
        )
        de = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="version",
            status="open",
            detected_at=now,
        )
        s.add(de)
        s.flush()
        ids = (de.id, r.id)
        s.commit()
    return ids


def _patch_structured(finding):
    """Patch rootcause.get_chat_model so with_structured_output(...).invoke
    returns *finding* (FakeListChatModel raises NotImplementedError on
    with_structured_output — recorded deviation, per the task brief)."""
    model = MagicMock()
    model.with_structured_output.return_value.invoke.return_value = finding
    return patch("infra_brain.agents.rootcause.get_chat_model", return_value=model)


def test_rootcause_is_llm_agent_with_role():
    from infra_brain.agents.llm_base import LLMAgent
    from infra_brain.agents.rootcause import RootCauseAgent

    assert issubclass(RootCauseAgent, LLMAgent)
    assert RootCauseAgent.llm_role == "rootcause"
    # spec-derived domain still survives LLMAgent.__init_subclass__'s TRK-060 check
    assert RootCauseAgent.domain == "rootcause"
    assert RootCauseAgent.spec.domain == "rootcause"


def test_flag_off_never_touches_llm(engine):
    """rootcause_llm_enabled=False -> deterministic path, reason() never called."""
    from infra_brain.agents.rootcause import RootCauseAgent

    _seed_event(engine)
    agent = RootCauseAgent.__new__(RootCauseAgent)
    agent.settings = MagicMock(rootcause_llm_enabled=False)
    agent.callbacks = []
    agent._llm = None
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        patch.object(RootCauseAgent, "reason") as reason,
        patch("infra_brain.agents.rootcause.get_chat_model") as gcm,
    ):
        outcome = agent.collect()
    reason.assert_not_called()
    gcm.assert_not_called()
    assert outcome.count_override == 1
    assert outcome.errors == []
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
    assert "octopus" in note.explanation


# ``test_flag_off_edge_confidence_085_and_kg2`` — DELETED (P5). It asserted
# that the deterministic path emitted its TRIGGERED_BY edge at the hardcoded
# 0.85 when trigger_source resolved to a concrete Resource. Both the edge and
# the hardcoded confidence went with ``_emit_trigger_edge`` (see its epitaph in
# agents/rootcause.py) — there is no longer a 0.85 anywhere in the OFF path to
# pin, because the deterministic note carries no confidence of its own. The
# note-writing half of that path is covered by the tests above.


def test_flag_off_note_written_when_trigger_unresolved(engine):
    """OFF mode: a trigger_source naming no concrete Resource still gets a note.

    This used to be ``test_flag_off_no_edge_when_trigger_unresolved``, whose
    point was the KG-2 invariant (no edge on an unresolved trigger). P5 deleted
    the edge, so the invariant is now vacuous — but the surviving half is worth
    keeping on its own: an unresolvable trigger must not cost the operator the
    human-readable note, which is this agent's actual output.
    """
    _seed_event(engine, trigger_source="ghost-pipeline", pipeline_resource=False)
    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        _make_agent().collect()
    with Session(engine) as s:
        assert s.query(RootCauseNote).count() == 1


def test_llm_mode_writes_finding_and_clamps_confidence(engine):
    """ON mode: note carries finding.explanation, the clamped confidence
    (clamp(finding.confidence, 0.05, 0.99)) lands on the note, finalization is
    audited.

    The clamp used to be asserted on the TRIGGERED_BY edge's ``confidence``
    kwarg. P5 deleted that edge, but the clamp itself is unchanged and is still
    observable: ``_apply_finding`` writes the same clamped value onto
    ``note.correlated["llm"]["confidence"]``. The assertion moved to where the
    value now lives; the behaviour under test did not change.
    """
    from infra_brain.agents.rootcause import FINALIZATION_ITERATION, RootCauseFinding

    _seed_event(engine, trigger_source="pipeline-x", pipeline_resource=True)
    finding = RootCauseFinding(
        explanation="Deploy of pipeline-x changed the version.",
        trigger_source="pipeline-x",
        confidence=0.999,
        correlated_run_ids=["run-1"],
    )
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis text")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        _patch_structured(finding) as gcm,
    ):
        outcome = agent.collect()
    assert outcome.count_override == 1
    assert outcome.errors == []
    # callbacks mandatory on the finalization model (CLAUDE.md #1)
    assert gcm.call_args.kwargs["callbacks"] is agent.callbacks
    assert gcm.call_args.kwargs["role"] == "rootcause"
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
        assert note.explanation == "Deploy of pipeline-x changed the version."
        assert note.correlated["llm"]["trigger_source"] == "pipeline-x"
        assert note.correlated["llm"]["confidence"] == 0.99  # clamped from 0.999
        assert note.correlated["llm"]["correlated_run_ids"] == ["run-1"]
        audit = s.query(AgentDecisionLog).filter_by(iteration=FINALIZATION_ITERATION).one()
        assert audit.agent == "RootCauseAgent"
        assert audit.domain == "rootcause"
        assert "pipeline-x" in audit.reasoning_text


def test_llm_mode_confidence_floor_clamp(engine):
    """Floor half of the clamp, asserted on the note (see the ceiling test)."""
    from infra_brain.agents.rootcause import RootCauseFinding

    _seed_event(engine, trigger_source="pipeline-x", pipeline_resource=True)
    finding = RootCauseFinding(
        explanation="Very unsure.", trigger_source="pipeline-x", confidence=0.0
    )
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        _patch_structured(finding),
    ):
        agent.collect()
    with Session(engine) as s:
        assert s.query(RootCauseNote).one().correlated["llm"]["confidence"] == 0.05


def test_llm_mode_note_written_when_trigger_unresolved(engine):
    """ON mode: an LLM trigger_source naming no concrete Resource still gets a
    note carrying that trigger_source.

    Was ``test_llm_mode_no_edge_when_trigger_unresolved`` (the KG-2 invariant on
    the TRIGGERED_BY edge). P5 deleted the edge, and with it the requirement
    that a trigger_source resolve to a ``resources`` row before it could be
    recorded at all — the note keeps the LLM's answer either way, which is
    strictly more than the edge could carry. That is what this now pins.
    """
    from infra_brain.agents.rootcause import RootCauseFinding

    _seed_event(engine, trigger_source="pipeline-x", pipeline_resource=True)
    finding = RootCauseFinding(
        explanation="Ghost did it.", trigger_source="ghost-resource", confidence=0.9
    )
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        _patch_structured(finding),
    ):
        agent.collect()
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
    assert note.explanation == "Ghost did it."
    # The unresolvable trigger is still recorded — the edge had to drop it.
    assert note.correlated["llm"]["trigger_source"] == "ghost-resource"


# ``test_llm_mode_edge_resolves_scoped_to_correlated_domain_not_unrelated_namesake``
# — DELETED (P5). It was a regression test for the TRIGGERED_BY edge's endpoint
# RESOLUTION: that ``_apply_finding`` scope its ``session.query(Resource)``
# name lookup to _CORRELATE_DOMAINS, so a vsphere VM sharing a pipeline's name
# could not be silently mis-linked. That lookup existed only to supply the
# edge's TO endpoint and was deleted with ``_emit_trigger_edge``; the note
# records ``finding.trigger_source`` as a string and resolves nothing, so the
# namesake collision this guarded against can no longer occur. There is no
# behaviour left to regress. If the edge is ever re-established as a collector
# declaration, the scoping requirement comes back with it and needs a new test
# against that declaration — do not restore this one, it tests deleted code.


def test_llm_mode_exception_falls_back_deterministic_partial(engine):
    """ON mode: per-event LLM failure -> deterministic note for that event,
    error recorded, outcome status 'partial'."""
    _seed_event(engine, trigger_source="pipeline-x", pipeline_resource=True)
    agent = _make_llm_agent()
    agent.reason = MagicMock(side_effect=RuntimeError("model down"))
    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        outcome = agent.collect()
    assert outcome.count_override == 1
    assert outcome.errors and "model down" in outcome.errors[0]
    assert outcome.status == "partial"
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
    assert "Likely triggered by octopus" in note.explanation


def test_llm_mode_event_cap(engine):
    """ON mode: events beyond rootcause_llm_max_events_per_run skip the LLM
    (deterministic fallback) but still get notes — no error, no partial."""
    from infra_brain.agents.rootcause import RootCauseFinding

    _seed_event(engine, name="web-20")
    _seed_event(engine, name="web-21")
    finding = RootCauseFinding(explanation="LLM note.", confidence=0.5)
    agent = _make_llm_agent(cap=1)
    agent.reason = MagicMock(return_value="analysis")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        _patch_structured(finding),
    ):
        outcome = agent.collect()
    assert agent.reason.call_count == 1  # capped
    assert outcome.count_override == 2  # both events still noted
    assert outcome.errors == []
    assert outcome.status == "ok"
    with Session(engine) as s:
        assert s.query(RootCauseNote).count() == 2


def test_llm_mode_idempotent_one_note_per_event(engine):
    """ON mode respects the one-note-per-event uq: second run is a no-op."""
    from infra_brain.agents.rootcause import RootCauseFinding

    _seed_event(engine)
    finding = RootCauseFinding(explanation="LLM note.", confidence=0.5)
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        _patch_structured(finding),
    ):
        agent.collect()
        agent.collect()
    assert agent.reason.call_count == 1  # second run found nothing unnoted
    with Session(engine) as s:
        assert s.query(RootCauseNote).count() == 1


def test_correlate_deploys_tool(engine):
    """The correlate_deploys tool returns JSON with the deploy candidate."""
    from infra_brain.agents.rootcause import correlate_deploys

    event_id, _ = _seed_event(engine, trigger_source="pipeline-x")
    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        out = correlate_deploys.func(drift_event_id=str(event_id))
    payload = json.loads(out)
    assert payload["top"]["domain"] == "octopus"
    assert payload["top"]["trigger_source"] == "pipeline-x"
    assert payload["candidates"]
    assert "octopus" in payload["explanation"]


def test_correlate_deploys_tool_bad_inputs(engine):
    from infra_brain.agents.rootcause import correlate_deploys

    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        assert "Invalid drift_event_id" in correlate_deploys.func(drift_event_id="not-a-uuid")
        assert "No drift event found" in correlate_deploys.func(drift_event_id=str(uuid.uuid4()))
        assert "Invalid drift_event_id" in correlate_deploys.func(drift_event_id=None)
        assert "Invalid drift_event_id" in correlate_deploys.func(drift_event_id=12345)


def test_load_unnoted_orders_oldest_detected_first(engine):
    """L-1: unnoted drift events are processed oldest-detected first — they've
    waited longest for a note."""
    from infra_brain.agents.rootcause import RootCauseAgent

    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-30", source="LinuxAgent")
        s.add(r)
        s.flush()
        newer = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="a",
            status="open",
            detected_at=now,
        )
        older = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="b",
            status="open",
            detected_at=now - timedelta(hours=2),
        )
        s.add_all([newer, older])
        s.commit()
        older_id, newer_id = older.id, newer.id

    agent = RootCauseAgent.__new__(RootCauseAgent)
    with Session(engine) as s:
        unnoted, _ = agent._load_unnoted(s)
    ids_in_order = [de.id for de, _ in unnoted]
    assert ids_in_order.index(older_id) < ids_in_order.index(newer_id)


def test_llm_mode_concurrent_note_does_not_poison_batch(engine):
    """M-1: if another run notes one event between phase 1 and phase 3 of the
    LLM path, the other event's note must still land — no exception should
    escape and no note should be lost to a rolled-back shared commit."""
    from infra_brain.agents.rootcause import RootCauseFinding

    event_id_a, _ = _seed_event(engine, name="web-40")
    event_id_b, _ = _seed_event(engine, name="web-41")
    finding = RootCauseFinding(explanation="LLM note.", confidence=0.5)
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis")

    orig_get_session = _session_factory
    state = {"calls": 0, "inserted": False}

    @contextmanager
    def _tracking_session_factory(eng):
        # Skip the very first session (phase 1's gather — event_id_b must
        # still be "unnoted" there). Insert the concurrent note the next time
        # a session opens (the per-event finalization audit log, or phase 3's
        # write session) — either way it lands before phase 3 attempts to
        # write, simulating a race between phases.
        state["calls"] += 1
        if state["calls"] > 1 and not state["inserted"]:
            state["inserted"] = True
            with Session(eng) as s:
                s.add(
                    RootCauseNote(
                        drift_event_id=event_id_b,
                        explanation="concurrent note from another run",
                        correlated={},
                    )
                )
                s.commit()
        with orig_get_session(eng) as s:
            yield s

    with (
        patch(
            "infra_brain.agents.rootcause.get_session",
            lambda: _tracking_session_factory(engine),
        ),
        _patch_structured(finding),
    ):
        outcome = agent.collect()

    assert outcome.errors == []
    with Session(engine) as s:
        note_a = s.query(RootCauseNote).filter_by(drift_event_id=event_id_a).one()
        note_b = s.query(RootCauseNote).filter_by(drift_event_id=event_id_b).one()
    assert note_a.explanation == "LLM note."
    # event_id_b keeps the concurrent note; the LLM path skipped it rather
    # than clobbering it or raising.
    assert note_b.explanation == "concurrent note from another run"


# ---------------------------------------------------------------------------
# TRK-119: structured-output bounded retry on the finalization call
# ---------------------------------------------------------------------------


def test_llm_finalization_retries_transient_then_succeeds(engine):
    """A transient parse failure on the finalization structured-output call is
    retried (bounded) rather than immediately dropping the event to the
    deterministic fallback — the second attempt's finding is used."""
    from langchain_core.exceptions import OutputParserException

    from infra_brain.agents.rootcause import RootCauseFinding

    _seed_event(engine, trigger_source="pipeline-x", pipeline_resource=True)
    finding = RootCauseFinding(
        explanation="Deploy of pipeline-x changed the version.",
        trigger_source="pipeline-x",
        confidence=0.7,
    )
    model = MagicMock()
    model.with_structured_output.return_value.invoke.side_effect = [
        OutputParserException("garbled structured output"),
        finding,
    ]
    agent = _make_llm_agent()
    agent.reason = MagicMock(return_value="analysis text")
    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        patch("infra_brain.agents.rootcause.get_chat_model", return_value=model),
    ):
        outcome = agent.collect()

    assert model.with_structured_output.return_value.invoke.call_count == 2  # retried once
    assert outcome.errors == []  # transient recovered — NOT a deterministic fallback
    with Session(engine) as s:
        note = s.query(RootCauseNote).one()
    # LLM finding used, not the deterministic "Likely triggered by ..." text.
    assert note.explanation == "Deploy of pipeline-x changed the version."


def test_write_failure_is_recorded_in_errors_and_demotes_status(engine):
    """#78: a genuine phase-3 note-write failure (e.g. a DB error unrelated to
    the concurrent-note race M-1 already handles) must land in
    CollectOutcome.errors -- not just a logger.warning with no trace on the
    run -- so status demotes from 'completed' to 'partial' per the file's own
    contract (errors non-empty + written>0 -> 'partial')."""
    from infra_brain.agents.llm_base import LLMBudgetExceededError
    from infra_brain.agents.rootcause import RootCauseAgent

    event_id_a, _ = _seed_event(engine, name="web-60")
    event_id_b, _ = _seed_event(engine, name="web-61")
    agent = _make_llm_agent()
    # Ceiling already blown before the first event -> both events skip the
    # LLM and fall back to the deterministic write path (no error recorded
    # by phase 2 itself -- see test_llm_mode_token_ceiling_falls_back_...).
    agent.reason = MagicMock(side_effect=LLMBudgetExceededError("ceiling reached"))

    real_apply_fallback = RootCauseAgent._apply_fallback

    def _apply_fallback_boom(self, session, de, ctx):
        if de.id == event_id_b:
            raise RuntimeError("db write failed")
        return real_apply_fallback(self, session, de, ctx)

    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        patch.object(RootCauseAgent, "_apply_fallback", _apply_fallback_boom),
    ):
        outcome = agent.collect()

    assert outcome.errors, "expected the write failure to be recorded in errors"
    assert any(str(event_id_b) in e for e in outcome.errors)
    assert outcome.count_override == 1  # event A's write still succeeded
    assert outcome.status == "partial"
    with Session(engine) as s:
        notes = s.query(RootCauseNote).all()
    assert len(notes) == 1
    assert notes[0].drift_event_id == event_id_a


# ---------------------------------------------------------------------------
# TRK-120: per-run token ceiling → graceful deterministic fallback
# ---------------------------------------------------------------------------


def test_llm_mode_token_ceiling_falls_back_deterministic_no_error(engine):
    """When the opt-in per-run token ceiling trips mid-run (reason() raises
    LLMBudgetExceededError), remaining events use the deterministic fallback,
    NO error is recorded, and the run stays 'completed' — graceful degradation,
    not failure."""
    from infra_brain.agents.llm_base import LLMBudgetExceededError

    _seed_event(engine, name="web-50", trigger_source="pipeline-x", pipeline_resource=True)
    _seed_event(engine, name="web-51", trigger_source="pipeline-x", pipeline_resource=True)
    agent = _make_llm_agent()
    # Ceiling already blown before the first event → every event falls back.
    agent.reason = MagicMock(side_effect=LLMBudgetExceededError("ceiling reached"))
    with patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)):
        outcome = agent.collect()

    assert outcome.errors == []  # ceiling is degradation, not an error
    assert outcome.status == "ok"  # not "partial"
    assert outcome.count_override == 2  # both events still noted deterministically
    with Session(engine) as s:
        notes = s.query(RootCauseNote).all()
    assert len(notes) == 2
    assert all("Likely triggered by octopus" in n.explanation for n in notes)
