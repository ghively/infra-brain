"""LearningFeedbackAgent — closes the learning loop.

The read side (Instincts/Observations/Scripts → agent prompts) already exists in
``learning.build_context``. This agent adds the missing write-back: it nudges
Instinct confidence from recent collection outcomes and demotes
repeatedly-failing generated scripts, so the system's priors actually improve
(or decay) over time. Deterministic, DB-only.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from infra_brain.agents.base import BaseAgent
from infra_brain.learning import prune_unreliable_scripts, record_domain_outcomes
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)


class LearningFeedbackAgent(BaseAgent):
    spec = AgentSpec(
        domain="learning_feedback",
        tier=Tier.REPORTER,
        schedule="30 5 * * 0",
        max_staleness=timedelta(days=8),
        skip_hook=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        adjusted = record_domain_outcomes()
        pruned = prune_unreliable_scripts()
        logger.info(
            "LearningFeedbackAgent: %d instinct(s) adjusted, %d script(s) demoted",
            adjusted,
            pruned,
        )
        return []
