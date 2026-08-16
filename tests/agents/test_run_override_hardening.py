"""M-1: every agent that OVERRIDES ``run()`` must keep the base-class hardening.

``ETLConnector.run()`` (etl/base.py) carries two safety properties that are easy
to lose the moment a collector writes its own ``run()``:

1. **SEC-2 DSN scrubbing.** Every string that reaches
   ``CollectionRun.error_message`` (persisted, readable via the dashboard/API)
   goes through :func:`infra_brain.etl.base.scrub_dsn` first. A DB-layer
   exception — e.g. ``sqlalchemy.exc.ArgumentError`` on a malformed
   ``POSTGRES_URL`` — embeds the FULL connection string, credentials included,
   in ``str(exc)``. Without the scrub those live credentials are persisted
   verbatim and served to anyone who can read the run row.
2. **TRK-106 finally-finalize.** The run row is finalized in a ``finally`` so
   that even a ``BaseException`` (``KeyboardInterrupt`` / ``SystemExit``, which
   ``except Exception`` does NOT catch) still writes a terminal status +
   ``finished_at``. Without it the row is stranded ``status="in_progress"``
   forever and only the hourly stale-run reaper eventually notices.

Both were added to the base class and never propagated to the four agents that
override ``run()``. This module asserts the invariant **behaviourally** — it
drives each override to fail for real and inspects the persisted
``CollectionRun`` row — and discovers the overriding agents **programmatically**
(``cls.run is not ETLConnector.run`` over ``AGENT_REGISTRY``) so a fifth
override added later is covered automatically instead of silently reintroducing
the defect.

Failure induction uses ``_call_with_timeout``, the shared seam every override
already routes its real work through (F-004.4). If a future override stops
using it, these tests fail loudly ("the induced failure never reached the run
row") rather than passing vacuously — that is the intended signal to either
route the new override through the shared seam or extend this harness.
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.config import get_settings
from infra_brain.db.models import CollectionRun
from infra_brain.etl.base import ETLConnector
from infra_brain.supervisor import AGENT_REGISTRY

from tests.support.pg import make_engine

# A DSN whose credentials must NEVER survive into CollectionRun.error_message.
_SECRET = "sup3rs3cr3t-pw"
_DSN = f"postgresql://ib_writer:{_SECRET}@db.internal.example:5432/infra_brain"
_DSN_HOST = "db.internal.example"


def _overriding_agents() -> list[tuple[str, type]]:
    """Every registered agent class that defines its own ``run()``.

    Deliberately derived from the live registry + an identity comparison
    against ``ETLConnector.run`` rather than a hardcoded name list — a new
    override must be covered without anyone remembering to edit this file.
    """
    return sorted(
        (
            (domain, cls)
            for domain, cls in AGENT_REGISTRY.items()
            if cls.run is not ETLConnector.run
        ),
        key=lambda pair: pair[0],
    )


def test_the_discovery_helper_actually_finds_overrides():
    """Guard the guard: an empty list would make every test below vacuous."""
    found = _overriding_agents()
    assert found, (
        "No run() overrides discovered — either AGENT_REGISTRY failed to "
        "resolve or the identity comparison against ETLConnector.run broke. "
        "Every test in this module would silently pass with nothing asserted."
    )


def _engine():
    eng = make_engine()
    return eng


def _session_factory(eng):
    factory = sessionmaker(bind=eng)

    @contextmanager
    def _get():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _get


@contextmanager
def _patched_sessions(cls, get_session):
    """Point every ``get_session`` this agent's run() can reach at the test DB.

    Patches the agent's own module plus the two shared bases, resolved by
    module path rather than a hardcoded list so a new override in a new module
    is handled without edits here.
    """
    import sys

    module_names = {cls.__module__, "infra_brain.etl.base", "infra_brain.agents.base"}
    with ExitStack() as stack:
        for name in sorted(module_names):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "get_session"):
                stack.enter_context(patch.object(module, "get_session", get_session))
        yield


def _make_agent(cls):
    """Build the agent without running ``__init__`` (no callbacks/LLM wiring).

    Mirrors ``tests/agents/conftest.py``'s ``make_agent`` but uses the REAL
    Settings object: the run() overrides read several concrete settings fields
    (``collection_disabled_domains``, netdiscovery's aggressiveness knobs), and
    a MagicMock would make those reads meaningless.
    """
    agent = cls.__new__(cls)
    agent.settings = get_settings()
    agent.callbacks = []
    agent._active_run_id = None
    # DiscoveryAgent sets this in its own __init__, which we bypass.
    agent._current_run_id = None
    return agent


def _run_row(eng, domain):
    with Session(eng) as s:
        rows = s.query(CollectionRun).filter_by(domain=domain).all()
        assert len(rows) == 1, (
            f"expected exactly one CollectionRun row for domain={domain!r}, got {len(rows)}"
        )
        row = rows[0]
        return {
            "status": row.status,
            "error_message": row.error_message,
            "finished_at": row.finished_at,
        }


@pytest.mark.parametrize(
    "domain,cls", _overriding_agents(), ids=lambda v: getattr(v, "__name__", v)
)
def test_run_override_scrubs_dsn_credentials_from_error_message(domain, cls):
    """SEC-2: a DSN-bearing exception must not persist credentials on the run row."""
    eng = _engine()
    get_session = _session_factory(eng)
    agent = _make_agent(cls)

    def _boom(*args, **kwargs):
        raise ValueError(f"could not translate host name: {_DSN}")

    agent._call_with_timeout = _boom

    with _patched_sessions(cls, get_session):
        try:
            agent.run(trigger_type="manual", scope="all")
        except Exception:
            # Whether the override swallows the failure into a CollectionResult
            # or lets it propagate is not what this test is about — the run row
            # is.
            pass

    row = _run_row(eng, domain)

    # Not vacuous: the induced failure really did reach the persisted row.
    assert row["error_message"], (
        f"{cls.__name__}.run() persisted no error_message for a failing run — "
        "the induced failure never reached the CollectionRun row, so the scrub "
        "assertion below would be vacuous. Route the override's work through "
        "self._call_with_timeout (the shared F-004.4 seam) or extend this test."
    )
    assert _DSN_HOST in row["error_message"], (
        f"{cls.__name__}.run() persisted an error_message that does not come "
        f"from the induced exception ({row['error_message']!r}) — cannot prove "
        "the scrub ran on the real failure path."
    )
    assert _SECRET not in row["error_message"], (
        f"{cls.__name__}.run() persisted live DSN credentials into "
        f"CollectionRun.error_message: {row['error_message']!r}. Route the "
        "message through infra_brain.etl.base.scrub_dsn() (see its docstring: "
        "'never remove a call site without an equivalent replacement')."
    )
    assert "[REDACTED]" in row["error_message"], (
        f"{cls.__name__}.run(): expected the scrub_dsn redaction marker in {row['error_message']!r}"
    )


@pytest.mark.parametrize(
    "domain,cls", _overriding_agents(), ids=lambda v: getattr(v, "__name__", v)
)
def test_run_override_finalizes_run_row_on_base_exception(domain, cls):
    """TRK-106: a BaseException exit must not strand the row as ``in_progress``."""
    eng = _engine()
    get_session = _session_factory(eng)
    agent = _make_agent(cls)

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt("operator hit ctrl-c mid-collection")

    agent._call_with_timeout = _interrupt

    with _patched_sessions(cls, get_session):
        with pytest.raises(KeyboardInterrupt):
            agent.run(trigger_type="manual", scope="all")

    row = _run_row(eng, domain)
    assert row["status"] != "in_progress", (
        f"{cls.__name__}.run() left CollectionRun stranded at status="
        "'in_progress' after a KeyboardInterrupt. `except Exception` does not "
        "catch BaseException — finalize the row in a `finally` (TRK-106), as "
        "ETLConnector.run() does."
    )
    assert row["finished_at"] is not None, (
        f"{cls.__name__}.run() left CollectionRun.finished_at NULL after a "
        "KeyboardInterrupt — the row is indistinguishable from a still-running "
        "collection."
    )
