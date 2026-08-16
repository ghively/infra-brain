"""Shared structured logging setup for every infra-brain process.

Both the FastAPI app (main.py's lifespan) and the scheduler entrypoint
(scheduler.run_scheduler_forever) call configure_logging() so every container emits
the same JSON-envelope log format to stdout — required for `docker logs`-based
triage (see /sweep-debug layer 4) to parse consistently across containers instead
of each process inventing its own ad-hoc text format.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# LogRecord attributes present on every record regardless of what the caller
# passed via `extra={...}`. Anything on the record NOT in this set came from
# an `extra` kwarg (or a rare custom attribute set directly on the record)
# and gets folded into the JSON envelope — see JsonFormatter.format below.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as a single-line JSON envelope.

    Any field passed via ``logger.info("msg", extra={"key": val})`` is
    passed through onto the envelope (OB-4 remainder) so callers can attach
    structured context (domain, run_id, sweep_id, ...) without it getting
    silently dropped — `extra` fields land as plain attributes on the
    LogRecord and previously never made it into the JSON output.
    """

    def format(self, record: logging.LogRecord) -> str:
        envelope = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in envelope:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            envelope[key] = value
        if record.exc_info:
            envelope["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(envelope)


class _TextFormatter(logging.Formatter):
    """Plain-text formatter for local dev (LOG_FORMAT=text) — no JSON envelope,
    just a readable line. Extra fields are appended as key=value pairs so
    they're not silently dropped, mirroring JsonFormatter's passthrough."""

    _base_fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        base = logging.Formatter(self._base_fmt).format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS
        }
        if extras:
            base += " " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        return base


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single stdout logging handler on the root logger.

    force=True guarantees this wins even if a library already attached a handler
    to the root logger first — the exact bug (F-002) that left the scheduler
    container emitting zero `docker logs` output.

    Format is JSON by default (required for `docker logs`-based triage — see
    /sweep-debug layer 4 — to parse consistently across containers). Set
    ``LOG_FORMAT=text`` (local dev only) to get plain-text lines instead.
    """
    logging.basicConfig(level=level, stream=sys.stdout, force=True)
    formatter: logging.Formatter
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "text":
        formatter = _TextFormatter()
    else:
        formatter = JsonFormatter()
    from infra_brain.request_context import RequestIdLogFilter  # noqa: PLC0415 — avoid import cycle at module load

    for handler in logging.root.handlers:
        handler.setFormatter(formatter)
        if not any(isinstance(f, RequestIdLogFilter) for f in handler.filters):
            handler.addFilter(RequestIdLogFilter())
