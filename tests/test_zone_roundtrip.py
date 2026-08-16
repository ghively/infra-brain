"""Task 4.2 — ZONE_CORPORATE constant round-trip.

infra-brain has a zone-slug split bug: the canonical value is "corpor" (all
model defaults + config use it), but a few learning-loop readers/writers
hardcode "corporate", so instincts written on one value are invisible to
readers filtering the other — the learning loop silently drops knowledge.

Test A guards the model default. Test B drives the real write path
(DriftLearningAgent.collect(), which writes Instinct.zone via
``getattr(resource, "zone", "corporate")``) and the real read path
(``learning.build_context`` -> ``LLMAgent.reason`` zone default), proving
they converge on the same zone value end-to-end.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import DriftEvent, Instinct, Resource, ZONE_CORPORATE

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


def test_resource_default_zone_is_zone_corporate(db):
    """A Resource created with zone unset persists with the canonical zone."""
    resource = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="host",
        name="host-a",
        source="test",
    )
    db.add(resource)
    db.commit()
    db.refresh(resource)

    assert ZONE_CORPORATE == "corpor"
    assert resource.zone == ZONE_CORPORATE


def test_drift_learning_write_is_visible_to_llm_agent_read(db):
    """Round-trip: DriftLearningAgent writes an Instinct for a default-zone
    Resource, then LLMAgent's build_context() read path (as used at
    reason()-time) must return it.

    Before the fix this fails because drift_learning.py falls back to the
    literal "corporate" while the canonical zone value is "corpor", and
    learning._read_instincts's dead default also drifts to "corporate" —
    a write/read mismatch that silently drops the instinct.
    """
    from infra_brain.agents.drift_learning import DriftLearningAgent
    from infra_brain.learning import build_context

    # Resource with zone left at the model default (no zone= passed).
    resource = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="host",
        name="host-b",
        source="test",
    )
    db.add(resource)
    db.flush()

    now = datetime.now(timezone.utc)
    # 20 DriftEvents on the same (resource, drift_type, field) combo: clears the
    # agent's _MIN_OCCURRENCES threshold (2) AND pushes the recency-weighted
    # confidence score above build_context's _MIN_CONFIDENCE (0.7) filter, so
    # the written Instinct is actually surfaced by the read path.
    for _ in range(20):
        db.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                drift_type="config",
                field="disk_usage",
                detected_at=now,
                status="open",
            )
        )
    db.commit()

    # --- Write path: drive the real DriftLearningAgent.collect() against
    # the in-memory session (module-level get_session patch, same pattern as
    # tests/test_learning.py).
    agent = DriftLearningAgent.__new__(DriftLearningAgent)
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=db)
    mock_cm.__exit__ = MagicMock(return_value=False)
    with patch("infra_brain.agents.drift_learning.get_session", return_value=mock_cm):
        agent.collect(scope="all")

    written = db.query(Instinct).filter(Instinct.promoted_by == "drift_learning").all()
    assert len(written) == 1, "DriftLearningAgent should have written exactly one Instinct"
    assert written[0].zone == ZONE_CORPORATE, (
        f"Instinct written with zone={written[0].zone!r}, expected {ZONE_CORPORATE!r}"
    )

    # --- Read path: drive the real build_context() reader (what LLMAgent.reason()
    # calls at reason()-time) using the same default-zone lookup LLMAgent uses.
    with patch("infra_brain.learning.get_session", return_value=mock_cm):
        ctx = build_context("DriftLearningAgent", "linux", zone=ZONE_CORPORATE)

    assert "disk_usage" in ctx, (
        "build_context() did not surface the Instinct written by DriftLearningAgent "
        "— zone mismatch between writer and reader is dropping the learning loop"
    )
