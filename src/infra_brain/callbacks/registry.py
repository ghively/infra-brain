from langchain_core.callbacks import BaseCallbackHandler
from infra_brain.callbacks.audit import AsyncAuditCallbackHandler, AuditCallbackHandler
from infra_brain.callbacks.readonly import ReadOnlyToolValidator
from infra_brain.callbacks.dlp import DLPCallbackHandler
from infra_brain.callbacks.observation import (
    AsyncObservationCallbackHandler,
    ObservationCallbackHandler,
)
from infra_brain.callbacks.langfuse_handler import maybe_langfuse_handler
from infra_brain.config import get_settings  # noqa: F401  # re-exported; tests patch this


def build_callbacks(
    agent_name: str,
    domain: str,
    whitelisted_post: list[str] | None = None,
) -> list[BaseCallbackHandler]:
    handlers = [
        AuditCallbackHandler(agent_name=agent_name, domain=domain),
        ReadOnlyToolValidator(agent_name=agent_name, whitelisted_post=whitelisted_post or []),
        DLPCallbackHandler(agent_name=agent_name),
        ObservationCallbackHandler(agent_name=agent_name, domain=domain),
    ]
    # Phase 4 Task 2 (OB-7): Langfuse joins the existing sync-handler
    # exception documented on build_async_callbacks() below — its handler is
    # sync-but-backgrounded (no inline network I/O, never raises into the
    # agent), so it is safe to append last on both the sync and async chains.
    langfuse_handler = maybe_langfuse_handler()
    if langfuse_handler is not None:
        handlers.append(langfuse_handler)
    return handlers


def build_async_callbacks(
    agent_name: str,
    domain: str,
    whitelisted_post: list[str] | None = None,
) -> list[BaseCallbackHandler]:
    """B2: same shape as ``build_callbacks()``, for the two call sites that run
    on the FastAPI event loop (``chat/agent.py``'s ``stream_response`` and
    ``api/routers/ui.py``'s ``custom_view``).

    ``AuditCallbackHandler``/``ObservationCallbackHandler`` are swapped for
    their async, batched counterparts (CLAUDE.md constraint #3: no sync DB
    commit per tool event on the event loop thread). ``ReadOnlyToolValidator``/
    ``DLPCallbackHandler`` stay the same sync handlers — their ``_deny()`` path
    must stay durably synchronous (a blocked call's denial record has to be
    written before the ``PermissionError`` raises), so they are intentionally
    NOT folded into the async batch writer.
    """
    handlers = [
        AsyncAuditCallbackHandler(agent_name=agent_name, domain=domain),
        ReadOnlyToolValidator(agent_name=agent_name, whitelisted_post=whitelisted_post or []),
        DLPCallbackHandler(agent_name=agent_name),
        AsyncObservationCallbackHandler(agent_name=agent_name, domain=domain),
    ]
    # Phase 4 Task 2 (OB-7): see build_callbacks() above — Langfuse's handler
    # is sync-but-backgrounded, joining the same documented sync-handler
    # exception as ReadOnlyToolValidator/DLPCallbackHandler on this async
    # chain. Appended last.
    langfuse_handler = maybe_langfuse_handler()
    if langfuse_handler is not None:
        handlers.append(langfuse_handler)
    return handlers
