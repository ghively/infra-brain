"""Per-request correlation ID (#88 — OB observability gap: no HTTP
request/correlation ID anywhere).

`request_id_var` is set for the duration of one HTTP request by the
middleware installed in main.py's create_app(). RequestIdLogFilter reads it
and stamps every LogRecord processed by the root handler (installed in
logging_config.configure_logging()) with a `request_id` attribute, which
JsonFormatter/`_TextFormatter` then pass through into the log envelope
exactly like any other `extra={}` field (see logging_config.py) — no
individual call site needs to pass request_id explicitly; this is
transparent correlation for the DURATION of the request, regardless of
which module logs.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def get_request_id() -> str | None:
    """The current request's correlation ID, or None outside any request."""
    return request_id_var.get()


class RequestIdLogFilter(logging.Filter):
    """Stamps `record.request_id` from the ambient ContextVar, when set.

    Installed on the root logger's handler by configure_logging(). A filter
    (not a formatter concern) because it must run for every record regardless
    of which formatter (Json vs text) is active, and filters — unlike
    formatters — can inspect/mutate the record before either formatter sees it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        if rid is not None:
            record.request_id = rid
        return True


def request_id_middleware_factory() -> Callable[
    [Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]
]:
    """Returns an `@app.middleware("http")`-compatible callable.

    A factory (not a bare function) so tests can mount a fresh instance into a
    minimal FastAPI app without depending on the full create_app() lifespan.
    Honors an incoming X-Request-ID header (client/upstream-proxy-supplied
    correlation), or generates a fresh uuid4 hex when absent. Always returns
    the resolved ID in the response header for client-side correlation, and
    resets the ContextVar on the way out so it never leaks into an unrelated
    later request sharing the same asyncio Task (defensive — Starlette gives
    each request its own Task, so this is belt-and-suspenders).
    """

    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        rid = incoming if incoming else uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    return request_id_middleware
