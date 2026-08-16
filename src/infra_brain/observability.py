"""Observability bootstrap for Infra Brain.

Call ``init_tracing(settings)`` at application startup.  When
``langsmith_tracing`` is enabled, it sets the four LangChain environment
variables that the LangSmith SDK reads automatically; otherwise it is a no-op.
"""

from __future__ import annotations

import logging
import os

from infra_brain.config import Settings

logger = logging.getLogger(__name__)


def init_tracing(settings: Settings | None = None) -> None:
    """Configure LangSmith tracing from *settings*.

    Parameters
    ----------
    settings:
        Application settings.  When *None*, ``get_settings()`` is called
        so call-sites that already have settings can pass them in without
        a second lookup.
    """
    if settings is None:
        from infra_brain.config import get_settings

        settings = get_settings()

    managed_vars = (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    )

    if not settings.langsmith_tracing:
        # A previously-enabled process (or a stale env from an earlier
        # settings reload) must not keep leaking these vars once tracing is
        # disabled — unset anything this function manages.
        for var in managed_vars:
            os.environ.pop(var, None)
        return

    # OB-7/TRK-042: the cloud LangSmith endpoint is no longer the default
    # (config.py langsmith_endpoint now defaults to ""). Refuse to enable
    # tracing without an explicit endpoint instead of silently egressing
    # trace data anywhere by default.
    if not settings.langsmith_endpoint:
        logger.warning(
            "init_tracing: langsmith_tracing is enabled but langsmith_endpoint "
            "is empty — explicit endpoint required, cloud default removed. "
            "Set LANGSMITH_ENDPOINT to a self-hosted/approved endpoint to "
            "enable tracing. Tracing NOT enabled."
        )
        # Same reasoning as above: don't leave stale vars set from a prior
        # enabled state when refusing to (re-)enable tracing now.
        for var in managed_vars:
            os.environ.pop(var, None)
        return

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint
