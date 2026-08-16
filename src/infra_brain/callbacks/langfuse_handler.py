"""Langfuse tracing wiring (Phase 4 Task 2, OB-7).

``maybe_langfuse_handler()`` is a lazy factory: it returns ``None`` unless
tracing is explicitly enabled AND fully configured, and it imports the
``langfuse`` package ONLY inside that enabled branch — a disabled deployment
never imports ``langfuse`` at all (verified by a ``sys.modules`` assertion in
tests/callbacks/test_langfuse_handler.py, same pattern as Phase 2's opt-in
gates).

Sync-handler exception: ``langfuse.langchain.CallbackHandler`` joins the
registry's existing documented sync-handler exception (see the comment on
``build_async_callbacks()`` in callbacks/registry.py, which already keeps
``ReadOnlyToolValidator``/``DLPCallbackHandler`` sync on the async chain).
Langfuse's handler is sync-but-backgrounded: its callback methods only
enqueue data onto Langfuse's own OTel batch span processor — no inline
network I/O happens on the calling thread, and construction/callback errors
are swallowed internally by the SDK rather than raised into the agent. It is
therefore safe to append to both ``build_callbacks()`` (thread-pool sync
path) and ``build_async_callbacks()`` (FastAPI event-loop path) without
violating CLAUDE.md constraint #2 (no blocking I/O on the event loop).

Masking: client-side scrubbing is wired via the Langfuse client's
``mask_otel_spans`` hook (export-stage OpenTelemetry span masking — verified
against the installed SDK, see docstring on
``langfuse.types.MaskOtelSpansFunction``). It reuses ``redact_pans()`` from
``callbacks/dlp.py`` (never modified here) so PAN-shaped content is scrubbed
before spans leave the process, mirroring the DLP callback's own PAN
scrubbing contract.
"""

import logging

from langchain_core.callbacks import BaseCallbackHandler

from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.config import get_settings

log = logging.getLogger(__name__)

# Process-wide handler memo (MEDIUM review finding): build_callbacks() runs on
# every agent instantiation (28 agents × runs); without this memo we'd
# construct a fresh Langfuse(...) client per build and rely on the SDK's
# undocumented per-public_key client dedup to avoid N exporter threads.
# Deliberately NOT caching the failure case (None) — a transient Langfuse
# outage self-heals on the next build instead of disabling tracing forever.
_HANDLER_MEMO: list = []  # empty = never built successfully; [handler] = built


def _mask_otel_spans(*, params):
    """Export-stage span masking: scrub PAN-shaped text out of span attributes.

    String attribute values are scrubbed directly; list/tuple attributes are
    scrubbed element-wise (OTel allows Sequence[str] values). Other types
    (bools, numbers) are left untouched. Spans with nothing to redact are
    omitted from the patch mapping entirely, per the mask_otel_spans contract.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        changed: dict = {}
        for key, value in span.attributes.items():
            if isinstance(value, str):
                redacted = redact_pans_preserving_uuids(value)
                if redacted != value:
                    changed[key] = redacted
            elif isinstance(value, (list, tuple)) and any(isinstance(v, str) for v in value):
                # OTel attributes may be Sequence[str] — scrub element-wise so a
                # PAN inside a future list-valued attribute cannot leak.
                redacted_seq = [redact_pans_preserving_uuids(v) if isinstance(v, str) else v for v in value]
                if list(redacted_seq) != list(value):
                    changed[key] = (
                        type(value)(redacted_seq) if isinstance(value, tuple) else redacted_seq
                    )
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)

    if not patches:
        return None
    return MaskOtelSpansResult(span_patches=patches)


def maybe_langfuse_handler() -> BaseCallbackHandler | None:
    """Return a Langfuse LangChain callback handler, or None if not configured.

    Returns None unless ``langfuse_enabled`` is True AND ``langfuse_host``,
    ``langfuse_public_key``, and ``langfuse_secret_key`` are all set. The
    ``langfuse`` package is imported only inside this enabled+configured
    branch, so a disabled/unconfigured deployment never touches it.

    Construction failures (bad host, SDK error, etc.) are caught and logged —
    tracing must never break agent execution — and None is returned so the
    caller's callback chain is unaffected.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    if not (
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    ):
        return None

    if _HANDLER_MEMO:
        return _HANDLER_MEMO[0]

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            mask_otel_spans=_mask_otel_spans,
        )
        handler = CallbackHandler()
        _HANDLER_MEMO.append(handler)
        return handler
    except Exception:
        log.warning(
            "Langfuse handler construction failed; tracing disabled for this process", exc_info=True
        )
        return None
