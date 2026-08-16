from unittest.mock import MagicMock, patch
from infra_brain.agents.learning_feedback import LearningFeedbackAgent


def test_learning_feedback_agent_domain():
    assert LearningFeedbackAgent.domain == "learning_feedback"


def test_learning_feedback_collect_returns_empty_list():
    agent = LearningFeedbackAgent.__new__(LearningFeedbackAgent)
    agent.settings = MagicMock()
    with (
        patch("infra_brain.agents.learning_feedback.record_domain_outcomes", return_value=3),
        patch("infra_brain.agents.learning_feedback.prune_unreliable_scripts", return_value=1),
    ):
        result = agent.collect("all")
    assert result == []


def test_learning_feedback_calls_both_learning_functions():
    agent = LearningFeedbackAgent.__new__(LearningFeedbackAgent)
    agent.settings = MagicMock()
    with (
        patch(
            "infra_brain.agents.learning_feedback.record_domain_outcomes", return_value=0
        ) as mock_record,
        patch(
            "infra_brain.agents.learning_feedback.prune_unreliable_scripts", return_value=0
        ) as mock_prune,
    ):
        agent.collect("all")
    mock_record.assert_called_once()
    mock_prune.assert_called_once()


def test_learning_feedback_handles_exception_gracefully():
    agent = LearningFeedbackAgent.__new__(LearningFeedbackAgent)
    agent.settings = MagicMock()
    with (
        patch(
            "infra_brain.agents.learning_feedback.record_domain_outcomes",
            side_effect=Exception("db error"),
        ),
        patch("infra_brain.agents.learning_feedback.prune_unreliable_scripts", return_value=0),
    ):
        try:
            agent.collect("all")
            assert False, "Expected exception to propagate"
        except Exception as e:
            assert "db error" in str(e)
