"""Tests for Task F4: build_context — inject prior learning into prompts."""

import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from infra_brain.db.models import Base, Instinct, Observation


@pytest.fixture(scope="module")
def mem_engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(mem_engine):
    with Session(mem_engine) as session:
        yield session
        session.rollback()


# ---------------------------------------------------------------------------
# build_context — reads instincts, observations, scripts
# ---------------------------------------------------------------------------


def test_build_context_includes_instinct_pattern(db):
    """build_context returns a string containing a seeded Instinct pattern."""
    from infra_brain.learning import build_context

    instinct = Instinct(
        domain="infra",
        pattern="Always check disk usage before patching",
        confidence=0.9,
        promoted_by="test",
    )
    db.add(instinct)
    db.commit()

    with patch("infra_brain.learning.get_session") as mock_gs:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=db)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_gs.return_value = mock_cm
        ctx = build_context("LLMAgent", "infra")

    assert "Always check disk usage before patching" in ctx


def test_build_context_includes_observation_pattern(db):
    """build_context returns a string containing a seeded Observation pattern."""
    from infra_brain.learning import build_context

    obs = Observation(
        agent="LLMAgent",
        tool="disk_check",
        domain="infra",
        pattern="disk_check used 5 times successfully",
        count=5,
    )
    db.add(obs)
    db.commit()

    with patch("infra_brain.learning.get_session") as mock_gs:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=db)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_gs.return_value = mock_cm
        ctx = build_context("LLMAgent", "infra")

    assert "disk_check used 5 times successfully" in ctx


def test_build_context_empty_db_returns_empty_string():
    """build_context returns '' when DB has no instincts/observations."""
    from infra_brain.learning import build_context

    # Use a fresh in-memory db with no rows
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as empty_db:
        with patch("infra_brain.learning.get_session") as mock_gs:
            mock_cm = MagicMock()
            mock_cm.__enter__ = MagicMock(return_value=empty_db)
            mock_cm.__exit__ = MagicMock(return_value=False)
            mock_gs.return_value = mock_cm
            ctx = build_context("LLMAgent", "empty_domain")

    assert ctx == ""


def test_build_context_error_returns_empty_string():
    """A DB failure in build_context must return '' — never raise."""
    from infra_brain.learning import build_context

    with patch("infra_brain.learning.get_session", side_effect=RuntimeError("db down")):
        ctx = build_context("LLMAgent", "infra")

    assert ctx == ""


def test_build_context_output_bounded():
    """build_context output must be bounded — not unbounded raw text."""
    from infra_brain.learning import build_context

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as seeded_db:
        for i in range(20):
            seeded_db.add(
                Instinct(
                    domain="infra",
                    pattern="x" * 200,
                    confidence=0.9,
                    promoted_by="test",
                )
            )
        seeded_db.commit()

        with patch("infra_brain.learning.get_session") as mock_gs:
            mock_cm = MagicMock()
            mock_cm.__enter__ = MagicMock(return_value=seeded_db)
            mock_cm.__exit__ = MagicMock(return_value=False)
            mock_gs.return_value = mock_cm
            ctx = build_context("LLMAgent", "infra")

    assert len(ctx) <= 3000  # reasonable upper bound


# ---------------------------------------------------------------------------
# reason() prepends build_context output
# ---------------------------------------------------------------------------


def test_reason_prepends_build_context_sentinel():
    """reason() must prepend build_context output to the task HumanMessage."""
    from infra_brain.agents.llm_base import LLMAgent
    from langchain_core.messages import AIMessage

    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []
    agent.domain = "infra"
    agent.llm = MagicMock()

    sentinel = "## What you've learned before\n- sentinel instinct"

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_session)
    mock_cm.__exit__ = MagicMock(return_value=False)

    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="done")]}

    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=mock_cm),
        patch("infra_brain.agents.llm_base.build_context", return_value=sentinel),
    ):
        agent.reason("do the work", tools=[], max_iters=3)

    # The HumanMessage content passed to the framework agent must contain the sentinel
    _, invoke_kwargs = fake_compiled.invoke.call_args
    invoke_input = fake_compiled.invoke.call_args[0][0]
    messages = invoke_input["messages"]
    human_content = messages[0].content
    assert sentinel in human_content
    assert "do the work" in human_content


def test_reason_no_prepend_when_build_context_empty():
    """reason() must NOT prepend anything when build_context returns ''."""
    from infra_brain.agents.llm_base import LLMAgent
    from langchain_core.messages import AIMessage

    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []
    agent.domain = "infra"
    agent.llm = MagicMock()

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_session)
    mock_cm.__exit__ = MagicMock(return_value=False)

    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="done")]}

    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=mock_cm),
        patch("infra_brain.agents.llm_base.build_context", return_value=""),
    ):
        agent.reason("plain task", tools=[], max_iters=3)

    invoke_input = fake_compiled.invoke.call_args[0][0]
    messages = invoke_input["messages"]
    assert messages[0].content == "plain task"
