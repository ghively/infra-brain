"""BaseAgent — the LLM-capable agent base.

Wave 4 item 4.1a (F-009 / ARCHITECTURE-RECOMMENDATIONS R1): the collection
machinery (run() status contract, upserts, snapshots, detail-table helpers,
CollectionResult, CollectorSkipped) lives in
``infra_brain.etl.base.ETLConnector``. ``BaseAgent`` adds ONLY the lazily
constructed chat model, for agents that reason with an LLM. Deterministic
collectors subclass ``ETLConnector`` directly (item 4.1b) and structurally
cannot construct a model.

Backward-compat: every name previously importable from this module is
re-exported below, so existing ``from infra_brain.agents.base import ...``
sites keep working unchanged.
"""

from langchain_core.language_models import BaseChatModel

from infra_brain.etl.base import (
    CollectOutcome,
    CollectionResult,
    CollectorSkipped,
    ETLConnector,
    count_drift_events_for_run,
)
from infra_brain.llm import get_chat_model

__all__ = [
    "BaseAgent",
    "CollectOutcome",
    "CollectionResult",
    "CollectorSkipped",
    "ETLConnector",
    "count_drift_events_for_run",
]


class BaseAgent(ETLConnector):
    # Logical model role for per-agent Bedrock model selection. None → global
    # default. LLM-using agents override this (e.g. "discovery", "sql").
    llm_role: str | None = None

    def __init__(self):
        super().__init__()
        # The model is constructed lazily: only reason()-style callers require
        # LLM credentials or pay for model setup.
        self._llm = None

    @property
    def llm(self) -> BaseChatModel:
        """Lazily construct and cache the chat model on first use.

        Provider (Anthropic or AWS Bedrock) is selected via ``LLM_PROVIDER``;
        callbacks carry read-only/DLP/audit enforcement into every call.
        """
        if self._llm is None:
            self._llm = get_chat_model(role=self.llm_role, callbacks=self.callbacks)
        return self._llm

    @llm.setter
    def llm(self, value) -> None:
        # Allows tests / callers to inject a model (e.g. a fake LLM).
        self._llm = value
