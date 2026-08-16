"""Tests for the learning feedback loop (outcomes → confidence / script pruning)."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import CollectionRun, GeneratedScript, Instinct

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@contextmanager
def _factory(engine):
    with Session(engine) as s:
        yield s


def test_failing_domain_decays_confidence(engine):
    with Session(engine) as s:
        s.add(Instinct(domain="cicd", pattern="p", confidence=0.80, promoted_by="t"))
        for _ in range(5):
            s.add(CollectionRun(domain="cicd", trigger_type="scheduled", status="failed"))
        s.commit()

    from infra_brain import learning

    with patch.object(learning, "get_session", lambda: _factory(engine)):
        changed = learning.record_domain_outcomes()
    assert changed == 1
    with Session(engine) as s:
        assert s.query(Instinct).one().confidence == pytest.approx(0.75)


def test_succeeding_domain_reinforces_confidence(engine):
    with Session(engine) as s:
        s.add(Instinct(domain="linux", pattern="p", confidence=0.80, promoted_by="t"))
        for _ in range(5):
            s.add(CollectionRun(domain="linux", trigger_type="scheduled", status="completed"))
        s.commit()

    from infra_brain import learning

    with patch.object(learning, "get_session", lambda: _factory(engine)):
        learning.record_domain_outcomes()
    with Session(engine) as s:
        assert s.query(Instinct).one().confidence == pytest.approx(0.82)


def test_prune_unreliable_scripts(engine):
    with Session(engine) as s:
        s.add(
            GeneratedScript(
                name="bad",
                language="bash",
                purpose="x",
                content="",
                content_sha256="a",
                created_by_agent="A",
                domain="linux",
                run_count=3,
                last_returncode=1,
                reusable=True,
            )
        )
        s.add(
            GeneratedScript(
                name="good",
                language="bash",
                purpose="x",
                content="",
                content_sha256="b",
                created_by_agent="A",
                domain="linux",
                run_count=3,
                last_returncode=0,
                reusable=True,
            )
        )
        s.commit()

    from infra_brain import learning

    with patch.object(learning, "get_session", lambda: _factory(engine)):
        pruned = learning.prune_unreliable_scripts()
    assert pruned == 1
    with Session(engine) as s:
        bad = s.query(GeneratedScript).filter_by(name="bad").one()
        good = s.query(GeneratedScript).filter_by(name="good").one()
        assert bad.reusable is False
        assert good.reusable is True


def test_agent_runs_both(engine):
    from infra_brain.agents.learning_feedback import LearningFeedbackAgent
    from infra_brain import learning

    agent = LearningFeedbackAgent.__new__(LearningFeedbackAgent)
    agent.settings = None
    agent.callbacks = []
    with patch.object(learning, "get_session", lambda: _factory(engine)):
        assert agent.collect() == []
