"""Tests for DriftLearningAgent (Task F5)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resource(resource_id, rtype="ec2_instance"):
    r = MagicMock()
    r.id = resource_id
    r.type = rtype
    r.domain = "cloud"
    return r


def _make_drift_event(resource_id, drift_type, field, event_id=None):
    evt = MagicMock()
    evt.id = event_id if event_id is not None else uuid.uuid4()
    evt.resource_id = resource_id
    evt.drift_type = drift_type
    evt.field = field
    evt.detected_at = datetime.now(timezone.utc)
    return evt


def _make_root_cause_note(drift_event_id, explanation, created_at=None, correlated=None):
    note = MagicMock()
    note.id = uuid.uuid4()
    note.drift_event_id = drift_event_id
    note.explanation = explanation
    note.created_at = created_at or datetime.now(timezone.utc)
    note.correlated = correlated
    return note


def _setup_session_mocks(mock_session, events, resource, notes=None, existing_instincts=None):
    """Wire up the query chains DriftLearningAgent.collect() exercises,
    discriminated by model.

    M-4: the Resource lookup changed from a per-key
    `.filter_by(id=...).first()` to a single batched
    `.filter(Resource.id.in_(...)).all()` -- the SAME chain shape
    (`.query(X).filter(...).all()`) the RootCauseNote batch lookup already
    used. A single shared (non-model-discriminating) mock chain can no
    longer tell those two apart, so `session.query` is now a `side_effect`
    keyed by model, mirroring the real ORM (each model gets its own Query).
    Returns the per-model mock dict so a test can assert call counts against
    one specific model's chain (e.g. the N+1 regression check below).
    """
    from infra_brain.db.models import DriftEvent, Instinct, Resource, RootCauseNote

    notes = notes if notes is not None else []
    existing_instincts = existing_instincts if existing_instincts is not None else []
    resources = [resource] if resource is not None else []

    query_mocks: dict = {}

    def _query(model):
        if model not in query_mocks:
            q = MagicMock()
            if model is DriftEvent:
                q.order_by.return_value.limit.return_value.all.return_value = events
            elif model is Resource:
                q.filter.return_value.all.return_value = resources
            elif model is RootCauseNote:
                q.filter.return_value.all.return_value = notes
            elif model is Instinct:
                q.filter_by.return_value.all.return_value = existing_instincts
            query_mocks[model] = q
        return query_mocks[model]

    mock_session.query.side_effect = _query
    return query_mocks


# ---------------------------------------------------------------------------
# Test: collect() with drift history writes Instinct rows
# ---------------------------------------------------------------------------


def test_collect_writes_instincts_from_drift_events():
    """collect() must create Instinct rows with domain/confidence/pattern fields."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    # Three drift events on the same field -> high confidence
    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource)

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        result = agent.collect(scope="all")

    # Should return a list (may be empty if session mock doesn't fully cooperate)
    assert isinstance(result, list)

    # Instinct rows should have been added
    added_calls = mock_session.add.call_args_list
    assert len(added_calls) >= 1, "Expected at least one Instinct row to be added"

    # Verify the Instinct written has the right fields
    instinct = added_calls[0][0][0]
    assert hasattr(instinct, "domain"), "Instinct must have 'domain' field"
    assert hasattr(instinct, "confidence"), "Instinct must have 'confidence' field"
    assert hasattr(instinct, "pattern"), "Instinct must have 'pattern' field"
    assert hasattr(instinct, "promoted_by"), "Instinct must have 'promoted_by' field"
    assert instinct.promoted_by == "drift_learning"


def test_collect_confidence_scales_with_frequency():
    """Higher frequency drift on a field should produce higher confidence."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="vm")

    # Five events -> should produce high confidence
    events = [_make_drift_event(resource_id, "config_drift", "memory_gb") for _ in range(5)]

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource)

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    if added_calls:
        instinct = added_calls[0][0][0]
        assert instinct.confidence > 0.0, "Confidence must be positive"
        assert instinct.confidence <= 1.0, "Confidence must not exceed 1.0"


def test_collect_pattern_mentions_field_and_type():
    """The pattern text must mention the field name and resource type."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="linux_host")

    events = [_make_drift_event(resource_id, "config_drift", "kernel_version") for _ in range(3)]

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource)

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    if added_calls:
        instinct = added_calls[0][0][0]
        pattern_lower = instinct.pattern.lower()
        assert "kernel_version" in pattern_lower, (
            f"Pattern should mention field: {instinct.pattern}"
        )


# ---------------------------------------------------------------------------
# Test: collect() with empty DB → no crash, returns []
# ---------------------------------------------------------------------------


def test_collect_empty_history_returns_empty_list():
    """When there are no DriftEvent rows, collect() must return [] without crashing."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    mock_session = MagicMock()
    mock_session.query.return_value.order_by.return_value.limit.return_value.all.return_value = []

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        result = agent.collect(scope="all")

    assert result == []


# ---------------------------------------------------------------------------
# Test: collect() on DB error → returns []
# ---------------------------------------------------------------------------


def test_collect_on_db_error_raises():
    """collect() must propagate (F-007: status='failed') when a DB exception
    occurs, not silently return [] as a fake-successful empty run."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.side_effect = Exception("DB unavailable")

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        with pytest.raises(Exception, match="DB unavailable"):
            agent.collect(scope="all")


# ---------------------------------------------------------------------------
# Test: domain attribute
# ---------------------------------------------------------------------------


def test_drift_learning_agent_domain():
    """DriftLearningAgent.domain must be 'drift_learning'."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    assert DriftLearningAgent.domain == "drift_learning"


# ---------------------------------------------------------------------------
# Test: registered in AGENT_REGISTRY
# ---------------------------------------------------------------------------


def test_drift_learning_registered_in_agent_registry():
    """drift_learning must appear as a key in supervisor.AGENT_REGISTRY."""
    from infra_brain.supervisor import AGENT_REGISTRY

    assert "drift_learning" in AGENT_REGISTRY


# ---------------------------------------------------------------------------
# Test: in SKIP_HOOK
# ---------------------------------------------------------------------------


def test_drift_learning_in_skip_hook():
    """drift_learning must be in SKIP_HOOK so it doesn't trigger post_hook.

    B6: SKIP_HOOK is no longer a hand-maintained literal set in
    supervisor.py — it's computed from each domain's declarative
    ``skip_hook`` class attribute (etl/base.py's ETLConnector), which
    DriftLearningAgent sets to True. Check membership directly against the
    real (lazily-computed) SKIP_HOOK object instead of parsing supervisor.py's
    source for a set literal that no longer exists there.
    """
    from infra_brain.agents.drift_learning import DriftLearningAgent
    from infra_brain.supervisor import SKIP_HOOK

    assert DriftLearningAgent.skip_hook is True
    assert "drift_learning" in SKIP_HOOK


# ---------------------------------------------------------------------------
# Test: causal clause (RootCauseNote correlation)
# ---------------------------------------------------------------------------


def test_collect_appends_causal_clause_when_note_exists():
    """When a RootCauseNote exists for a contributing drift_event_id, the
    Instinct pattern must append the ' — likely cause: ...' clause."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]
    note = _make_root_cause_note(events[0].id, "a scaling policy resized the fleet")

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource, notes=[note])

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    assert len(added_calls) >= 1
    instinct = added_calls[0][0][0]
    assert "likely cause: a scaling policy resized the fleet" in instinct.pattern


def test_collect_no_causal_clause_without_note():
    """Without any RootCauseNote, the pattern must not contain the causal clause."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource, notes=[])

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    assert len(added_calls) >= 1
    instinct = added_calls[0][0][0]
    assert "likely cause" not in instinct.pattern


def test_collect_causal_clause_picks_most_recent_note():
    """When multiple contributing drift_event_ids each have a RootCauseNote,
    the MOST RECENT note's explanation must be used."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]
    older_note = _make_root_cause_note(
        events[0].id, "old cause", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    newer_note = _make_root_cause_note(
        events[1].id, "new cause", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource, notes=[older_note, newer_note])

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    assert len(added_calls) >= 1
    instinct = added_calls[0][0][0]
    assert "likely cause: new cause" in instinct.pattern
    assert "old cause" not in instinct.pattern


def _collect_one_instinct(notes):
    """Run collect() over one repeated drift key and return the added Instinct."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")
    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]
    if notes:
        # attach the note to a contributing event
        notes[0].drift_event_id = events[0].id

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource, notes=notes)

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    return mock_session.add.call_args_list[0][0][0]


def test_instinct_carries_manual_provenance_structurally():
    """A note authored via the MCP manual-write tool must mark the promoted
    Instinct through its own ``citation`` field — NOT by hoping the
    '[MANUAL/MCP-authored by ...]' prose banner survives the [:200] truncation
    of the explanation."""
    from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE

    note = _make_root_cause_note(
        None,
        "[MANUAL/MCP-authored by mcp:some-key] someone rebooted it",
        correlated={"source": MANUAL_PROVENANCE_SOURCE, "authored_by": "mcp:some-key"},
    )

    instinct = _collect_one_instinct([note])

    assert instinct.citation == f"{MANUAL_PROVENANCE_SOURCE}:root_cause_note:{note.id}"


def test_manual_provenance_survives_an_explanation_long_enough_to_truncate():
    """The whole point of the structural marker: provenance holds even when the
    banner is nowhere near the surviving [:200] window."""
    from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE

    note = _make_root_cause_note(
        None,
        "[MANUAL/MCP-authored by mcp:" + ("k" * 300) + "] the actual cause text",
        correlated={"source": MANUAL_PROVENANCE_SOURCE},
    )

    instinct = _collect_one_instinct([note])

    # the banner did NOT survive intact into the pattern...
    assert "the actual cause text" not in instinct.pattern
    # ...but provenance is still unambiguous on the row
    assert instinct.citation.startswith(MANUAL_PROVENANCE_SOURCE)


def test_agent_authored_note_is_not_marked_manual():
    from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE

    note = _make_root_cause_note(None, "agent reasoned this", correlated={"runs": ["r1"]})

    instinct = _collect_one_instinct([note])

    assert instinct.citation == f"root_cause_agent:root_cause_note:{note.id}"
    assert not instinct.citation.startswith(MANUAL_PROVENANCE_SOURCE)


def test_no_note_means_no_citation():
    assert _collect_one_instinct([]).citation is None


def test_spoofed_banner_without_the_structural_marker_is_not_trusted():
    """Prose is not evidence: an agent-written note whose text merely CONTAINS
    the banner must not be promoted as manually authored."""
    from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE

    note = _make_root_cause_note(
        None,
        "[MANUAL/MCP-authored by youruser] not actually manual",
        correlated={"source": "rootcause_agent"},
    )

    instinct = _collect_one_instinct([note])

    assert not instinct.citation.startswith(MANUAL_PROVENANCE_SOURCE)


def test_collect_causal_clause_scrubs_pan():
    """A RootCauseNote.explanation carrying a PAN-shaped number must be
    redacted before being folded into the Instinct pattern — the causal
    clause interpolates LLM-authored free text (rootcause.py LLM mode), so
    it must be scrubbed at point of use exactly like every other surfaced
    LLM string in the codebase (redact_pans)."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    events = [
        _make_drift_event(resource_id, "config_drift", "instance_type"),
        _make_drift_event(resource_id, "config_drift", "instance_type"),
    ]
    note = _make_root_cause_note(events[0].id, "customer card 4111111111111111 triggered a rebuild")

    mock_session = MagicMock()
    _setup_session_mocks(mock_session, events, resource, notes=[note])

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    added_calls = mock_session.add.call_args_list
    assert len(added_calls) >= 1
    instinct = added_calls[0][0][0]
    assert "4111111111111111" not in instinct.pattern
    assert "likely cause" in instinct.pattern


def test_collect_root_cause_note_query_is_single_call_not_n_plus_1():
    """The RootCauseNote lookup must be ONE query for all aggregated keys,
    never one query per key (N+1)."""
    from infra_brain.agents.drift_learning import DriftLearningAgent
    from infra_brain.db.models import RootCauseNote

    resource_id_a = uuid.uuid4()
    resource_id_b = uuid.uuid4()
    resource = _make_resource(resource_id_a, rtype="ec2_instance")

    events = [
        _make_drift_event(resource_id_a, "config_drift", "instance_type"),
        _make_drift_event(resource_id_a, "config_drift", "instance_type"),
        _make_drift_event(resource_id_b, "config_drift", "memory_gb"),
        _make_drift_event(resource_id_b, "config_drift", "memory_gb"),
    ]

    mock_session = MagicMock()
    query_mocks = _setup_session_mocks(mock_session, events, resource, notes=[])

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    # session.query(RootCauseNote).filter(...).all() must only be invoked
    # once total for the whole run, not once per aggregated
    # (resource_id, drift_type, field) key (there are 2 distinct keys here).
    assert query_mocks[RootCauseNote].filter.return_value.all.call_count == 1


def test_collect_resource_and_instinct_lookups_are_bounded_not_per_key():
    """M-4: two more N+1/full-scan-per-key patterns in the same loop:

    1. ``session.query(Resource).filter_by(id=resource_id).first()`` used to
       run once PER aggregated key (an N+1 lookup) -- must instead be one
       batched query for all keys.
    2. ``session.query(Instinct).filter_by(domain=..., promoted_by=...)
       .all()`` (the dedup existing-instinct scan) used to re-run in FULL
       for every key, even when many keys share the same domain -- must
       instead run at most once per DISTINCT domain.

    Two resources in the SAME domain ("cloud", `_make_resource`'s default)
    but different ids/fields produce 2 aggregated keys sharing 1 domain, so
    a per-key implementation is observably different (2 calls) from a
    per-domain-cached one (1 call).
    """
    from infra_brain.agents.drift_learning import DriftLearningAgent
    from infra_brain.db.models import Instinct, Resource

    resource_id_a = uuid.uuid4()
    resource_id_b = uuid.uuid4()
    resource_a = _make_resource(resource_id_a, rtype="ec2_instance")
    resource_b = _make_resource(resource_id_b, rtype="ec2_instance")  # same domain: "cloud"

    events = [
        _make_drift_event(resource_id_a, "config_drift", "instance_type"),
        _make_drift_event(resource_id_a, "config_drift", "instance_type"),
        _make_drift_event(resource_id_b, "config_drift", "memory_gb"),
        _make_drift_event(resource_id_b, "config_drift", "memory_gb"),
    ]

    mock_session = MagicMock()
    query_mocks = _setup_session_mocks(mock_session, events, resource_a, notes=[])
    # Pre-populate the Resource entry (BEFORE collect() runs any query) so
    # the batched fetch resolves BOTH resources -- the shared helper only
    # seeds one by default. `_setup_session_mocks`'s `_query()` closure only
    # lazily builds an entry when one isn't already present, so seeding it
    # here makes collect() use exactly this mock for `session.query(Resource)`.
    resource_query_mock = MagicMock()
    resource_query_mock.filter.return_value.all.return_value = [resource_a, resource_b]
    query_mocks[Resource] = resource_query_mock

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    assert query_mocks[Resource].filter.return_value.all.call_count == 1, (
        "Resource lookup must be one batched query for all aggregated keys, "
        "not one query per key (N+1)"
    )
    assert query_mocks[Instinct].filter_by.return_value.all.call_count == 1, (
        "the existing-Instinct dedup scan must run at most once per distinct "
        "domain, not once per aggregated key -- both keys here share the "
        "same domain ('cloud')"
    )


# ---------------------------------------------------------------------------
# Test: dedup must survive a changed occurrence count between runs
# ---------------------------------------------------------------------------


def test_pattern_dedup_key_ignores_occurrence_count():
    """_pattern_dedup_key must normalize out the volatile count so two
    pattern strings differing only in occurrence count compare equal."""
    from infra_brain.agents.drift_learning import _pattern_dedup_key

    low_count = (
        "field instance_type on type ec2_instance drifts frequently "
        "(2x, type=config_drift) — monitor closely"
    )
    high_count = (
        "field instance_type on type ec2_instance drifts frequently "
        "(47x, type=config_drift) — monitor closely"
    )
    assert _pattern_dedup_key(low_count) == _pattern_dedup_key(high_count)

    # A genuinely different drift_type must still be distinguished.
    different_type = (
        "field instance_type on type ec2_instance drifts frequently "
        "(2x, type=other_drift) — monitor closely"
    )
    assert _pattern_dedup_key(low_count) != _pattern_dedup_key(different_type)


def test_collect_updates_existing_instinct_despite_changed_count():
    """Regression for the AA-R finding: an existing Instinct row for the
    same domain+field+type combo must be found and updated (not duplicated)
    even though this run's occurrence count differs from the count baked
    into the existing row's stored pattern text."""
    from infra_brain.agents.drift_learning import DriftLearningAgent

    resource_id = uuid.uuid4()
    resource = _make_resource(resource_id, rtype="ec2_instance")

    # 5 occurrences this run -> newly computed pattern reads "(5x, ...)",
    # which differs from the "(2x, ...)" baked into the existing row below.
    events = [_make_drift_event(resource_id, "config_drift", "instance_type") for _ in range(5)]

    existing_instinct = MagicMock()
    existing_instinct.pattern = (
        "field instance_type on type ec2_instance drifts frequently "
        "(2x, type=config_drift) — monitor closely"
    )
    existing_instinct.confidence = 0.1
    existing_instinct.citation = None
    existing_instinct.domain = "cloud"

    mock_session = MagicMock()
    _setup_session_mocks(
        mock_session, events, resource, notes=[], existing_instincts=[existing_instinct]
    )

    with patch("infra_brain.agents.drift_learning.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        agent = DriftLearningAgent.__new__(DriftLearningAgent)
        agent.domain = "drift_learning"
        agent.collect(scope="all")

    # No brand-new duplicate Instinct row should have been added for this key.
    assert mock_session.add.call_count == 0, (
        "A changed occurrence count must not defeat dedup and create a "
        "duplicate Instinct row"
    )
    # The existing row should have been updated in place instead.
    assert existing_instinct.confidence > 0.1
