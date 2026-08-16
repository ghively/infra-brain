"""ETLConnector — base class for deterministic collection connectors.

Moved verbatim from agents/base.py (Wave 4 item 4.1a). Holds the run() status
contract, resource/snapshot upserts, and the detail-table write helpers.
Deliberately imports neither infra_brain.llm nor langgraph: a connector is
structurally unable to construct a model or a graph (F-009, F-004.4/R1).
"""

import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from typing import ClassVar

from infra_brain.callbacks.registry import build_callbacks
from infra_brain.config import get_settings
from infra_brain.db.models import CollectionRun, DriftEvent, Resource, Snapshot
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec

logger = logging.getLogger(__name__)

# Phase 2 Task 1 (orchestration v2.1 groundwork): run-state constants for the
# sweep graph's retry/interrupt handling, defined here now so Task 2/Phase 3
# can reference stable names without a follow-up rename. Not yet written by
# any code path — CollectionRun.status keeps using the existing
# "in_progress"/"completed"/"partial"/"failed"/"skipped" literals until the
# sweep graph lands.
RUN_STATUS_RETRY_EXHAUSTED = "retry_exhausted"
RUN_STATUS_INTERRUPT_PENDING = "interrupt_pending"

# SEC-2: matches the CREDENTIALS portion of a DSN/connection-string —
# ``scheme://user[:pass]@`` — e.g. a malformed POSTGRES_URL raising
# sqlalchemy.exc.ArgumentError with the full string, including credentials,
# embedded verbatim in str(exc). Deliberately scoped to the "user[:pass]@"
# segment (not the whole URL): most collectors talk plain HTTPS APIs
# (Octopus/Rapid7/GitLab/...) with no embedded credentials, and a plain
# ``https://host/path`` error is genuinely useful for triage — over-redacting
# every "scheme://" match would blind operators to which endpoint failed for
# no security benefit, since no credential is present in that case.
_DSN_CREDENTIALS_RE = re.compile(r"([a-zA-Z][\w+]*://)([^/\s@]+@)")


def scrub_dsn(message: str | None) -> str | None:
    """Redact embedded DSN credentials (``scheme://user:pass@...``) from an error message.

    SEC-2: every collector's failure path funnels an exception's ``str()`` into
    ``CollectionRun.error_message`` (persisted, readable via the dashboard/API)
    and ``CollectionResult.errors`` — a shared choke point. If that exception
    originated from a DB-layer failure (e.g. a malformed DSN raising
    ``sqlalchemy.exc.ArgumentError``, whose message embeds the full connection
    string including any credentials), the raw credentials would otherwise be
    persisted and exposed verbatim. This is applied at every site that
    stores/returns a collector's exception message — never remove a call site
    without an equivalent replacement.
    """
    if not message:
        return message
    return _DSN_CREDENTIALS_RE.sub(r"\1[REDACTED]@", message)


class ScrubbedErrorList(list):
    """A ``list[str]`` that applies :func:`scrub_dsn` to everything put into it.

    M-1/SEC-2. ``ETLConnector.run()`` has exactly ONE place where a collector
    error string enters the run (``errors.extend(scrub_dsn(e) for e in
    outcome.errors)``), so a single explicit call site is enough there. A
    ``run()`` override is different: netdiscovery alone accumulates failures at
    nine scattered ``errors.append``/``errors.extend`` sites across five tiers,
    and every one of them feeds BOTH ``CollectionResult.errors`` (returned to
    dispatch/the sweep summary) and the joined ``CollectionRun.error_message``.
    Requiring nine correct call sites — and a tenth when a new tier is added —
    is exactly the discipline that failed here in the first place.

    Making the CONTAINER enforce the invariant means a new append site cannot
    forget. Non-str items pass through untouched. ``scrub_dsn`` is idempotent,
    so double-scrubbing an already-scrubbed string is harmless.
    """

    def append(self, item):  # noqa: D102 - inherits list.append semantics
        super().append(scrub_dsn(item) if isinstance(item, str) else item)

    def extend(self, items):  # noqa: D102
        for item in items:
            self.append(item)

    def insert(self, index, item):  # noqa: D102
        super().insert(index, scrub_dsn(item) if isinstance(item, str) else item)

    def __iadd__(self, items):
        self.extend(items)
        return self


# CollectionRun.error_message is an UNBOUNDED Postgres TEXT column (see
# db/models/core.py's `mapped_column(Text, nullable=True)` -- no VARCHAR(n)
# constraint anywhere in the alembic history either). There is no DB-imposed
# width limit being honored here -- this is purely a self-imposed practical
# cap, sized generously above realistic per-domain error volumes so real
# cases are never truncated. It exists only to bound genuinely pathological
# error counts (e.g. an unbounded per-item failure loop producing thousands
# of entries) from growing the column and dashboard payload without limit.
# Confirmed-live reference point: homelab_services, 32 skipped-entry errors,
# ~300 chars each = ~9800 chars joined -- comfortably under this cap, so it
# is never truncated. This keeps only complete entries (never truncates
# mid-entry) and appends an explicit count of what was omitted.
_ERROR_MESSAGE_MAX_CHARS = 20000
_ERROR_MESSAGE_BUDGET_CHARS = 19900  # leaves room for the "; ...and N more" suffix


def _join_errors_truncated(errors: list[str]) -> str:
    joined = "; ".join(errors)
    if len(joined) <= _ERROR_MESSAGE_MAX_CHARS:
        return joined

    included: list[str] = []
    total_len = 0
    for e in errors:
        sep_len = 2 if included else 0  # "; "
        if total_len + sep_len + len(e) > _ERROR_MESSAGE_BUDGET_CHARS:
            break
        included.append(e)
        total_len += sep_len + len(e)

    omitted = len(errors) - len(included)
    result = "; ".join(included)
    if omitted:
        suffix = f"...and {omitted} more"
        result = f"{result}; {suffix}" if result else suffix
    return result


class CollectorSkipped(Exception):
    """Raised by collect() when the collector is intentionally a no-op.

    Use this when the collector cannot run because a required dependency is
    unconfigured (e.g. no SNMP targets, no kubeconfig, aws_enabled=False).
    BaseAgent.run() catches CollectorSkipped and records status="skipped" with
    the reason — distinct from status="completed" (ran, found nothing) and
    status="failed" (runtime error).

    Example::

        def collect(self, scope: str) -> list[dict]:
            if not self.settings.snmp_targets:
                raise CollectorSkipped("no SNMP targets configured")
            ...
    """


def count_drift_events_for_run(run_id: uuid.UUID, session) -> int:
    """Return the number of DriftEvent rows attributable to the given collection run."""
    return session.query(DriftEvent).filter(DriftEvent.collection_run_id == run_id).count()


@dataclass
class CollectOutcome:
    """Typed result of a collect() phase — R3 contract (ok/partial/failed).

    Status mapping applied by ETLConnector.run():
      ok      (no errors)                          -> CollectionRun.status "completed"
      partial (errors AND items/count_override>0)  -> "partial"
      failed  (errors AND no data)                 -> "failed"
    "skipped" is unchanged: collectors raise CollectorSkipped.

    ``count_override`` lets detail-writer agents (rootcause, vuln_triage,
    compliance) report a truthful resources_found without emitting generic
    Resource rows (F-008). Legacy collectors may keep returning a bare
    list[dict]; run() treats that as ok.
    """

    items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    count_override: int | None = None

    @property
    def status(self) -> str:
        if not self.errors:
            return "ok"
        if self.items or (self.count_override or 0) > 0:
            return "partial"
        return "failed"


@dataclass
class ReconcileScope:
    """Proven-observed scope guard for destructive reconciliation passes (C-1).

    **The failure mode this exists to prevent.** A collector fans out one fetch
    per unit (asset, host, project, namespace, ...), then runs a *reconciliation*
    pass over the units it "scanned": close / prune / NULL-out / delete-then-
    reinsert every stored row that the fresh data no longer mentions. The pass is
    only sound if "the fresh data no longer mentions it" is a *measurement*. When
    a per-unit fetch raises and the collector swallows it into an empty list, that
    unit's empty result is indistinguishable from "upstream genuinely reports zero
    rows here" — and the reconciliation pass then deletes/closes the unit's ENTIRE
    stored set on the strength of a transient 500. The live instance of this
    (``agents/vuln.py::_write_vuln_queue``): one Rapid7 timeout on one asset
    marked that host's whole open-CVE set ``resolved``, dropped the dashboard's
    "Open CVEs" count, and still reported the run ``completed`` with no errors.

    **The contract.** Record every unit as either :meth:`observed` (its fetch
    succeeded — reconciling it is sound) or :meth:`failed` (its fetch raised —
    reconciling it is NOT sound). Then scope the destructive pass to
    :attr:`safe_scope` and *only* :attr:`safe_scope`::

        scope = ReconcileScope(label="asset")
        for unit_id, rows, exc in fetched:
            if exc is None:
                scope.observed(unit_id)
            else:
                scope.failed(unit_id, exc)

        # destructive pass — proven-observed units ONLY
        stale = session.query(Row).filter(Row.unit_id.in_(scope.safe_scope), ...)

        # and the failures must not vanish
        return CollectOutcome(items=items, errors=scope.errors)

    Feeding :attr:`errors` into ``CollectOutcome.errors`` is the whole reporting
    story: :attr:`CollectOutcome.status` already maps errors-plus-data to
    ``"partial"`` and errors-with-no-data to ``"failed"``, which ``run()`` writes
    straight onto ``CollectionRun.status``. There is deliberately NO separate
    signalling channel here — a swallowed fetch failure must show up on the run,
    and this reuses the mechanism that already does that.

    **Fail-closed semantics.** A failure is *sticky*: a key passed to
    :meth:`failed` never appears in :attr:`safe_scope`, even if :meth:`observed`
    is also called for it (in either order). A partially-observed unit cannot
    prove the *absence* of a row, and absence is exactly what a reconciliation
    pass asserts when it closes or deletes. Skipping a unit this cycle is
    cheap and self-correcting; deleting live data is neither.

    Error strings are passed through :func:`scrub_dsn` (SEC-2) because they land
    in ``CollectionRun.error_message``, which is persisted and rendered on the
    dashboard.
    """

    #: Noun used in the error strings, e.g. ``label="asset"`` ->
    #: ``"asset 42 fetch failed: RuntimeError: HTTP 500"``.
    label: str = "unit"

    _observed: set = field(default_factory=set, repr=False)
    _failed: set = field(default_factory=set, repr=False)
    _errors: list[str] = field(default_factory=list, repr=False)

    def observed(self, key) -> None:
        """Record that *key*'s fetch SUCCEEDED — reconciling it is sound.

        Idempotent. Has no effect on membership in :attr:`safe_scope` if *key*
        has also been passed to :meth:`failed` (failures are sticky).
        """
        hash(key)  # fail loudly here, not later inside the set/query
        self._observed.add(key)

    def failed(self, key, exc: BaseException | str) -> None:
        """Record that *key*'s fetch FAILED — it must NOT be reconciled.

        Permanently removes *key* from :attr:`safe_scope` and appends one entry
        to :attr:`errors`. Every call appends (a retry that fails twice reports
        twice), so the error list is a faithful log of what went wrong.
        """
        hash(key)
        self._failed.add(key)
        if isinstance(exc, BaseException):
            text = str(exc)
            detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
        else:
            detail = str(exc)
        self._errors.append(scrub_dsn(f"{self.label} {key} fetch failed: {detail}"))

    @property
    def safe_scope(self) -> set:
        """The keys whose fetch is PROVEN to have succeeded — scope destructive
        passes to exactly this, never to the full input list."""
        return self._observed - self._failed

    @property
    def errors(self) -> list[str]:
        """Error strings suitable for ``CollectOutcome.errors`` (already scrubbed)."""
        return list(self._errors)

    @property
    def failed_keys(self) -> set:
        """The keys whose fetch failed (for logging/metrics — never for scoping)."""
        return set(self._failed)

    @property
    def has_failures(self) -> bool:
        return bool(self._failed)

    @property
    def observed_count(self) -> int:
        return len(self.safe_scope)

    @property
    def failed_count(self) -> int:
        return len(self._failed)


@dataclass
class CollectionResult:
    run_id: uuid.UUID
    domain: str
    resources_found: int
    drift_count: int
    # One of: "completed" | "partial" | "failed" | "skipped"
    status: str
    errors: list[str] = field(default_factory=list)
    detail_rows_written: int = 0
    # B3: the scope the collection ran with (e.g. "all", "pulse", "health").
    # supervisor.py's _post_collection_hook branches on this — a "pulse" run
    # (vsphere's 15-min power-state/quickStats refresh) must NOT trigger a
    # full-fleet-equivalent drift scan every 15 minutes.
    scope: str = "all"


class ETLConnector(ABC):
    domain: str = "base"

    # Phase 1 Task 5 (TRK-047): the single declarative registry entry. A
    # concrete agent class declares ONLY ``spec = AgentSpec(...)``;
    # __init_subclass__ below derives the legacy class attributes
    # (domain/schedule/skip_hook/dispatchable) from it so supervisor.py and
    # scheduler.py — which read those attributes — keep working unmodified.
    # ETLConnector itself (and intermediate bases like LLMAgent) declare no
    # spec, so the derivation only fires on classes that define one.
    spec: ClassVar[AgentSpec | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        spec = cls.__dict__.get("spec")
        if spec is not None:
            # NOTE: when a class body declares BOTH `spec` and a direct legacy
            # attr (domain/schedule/skip_hook/dispatchable), the spec value wins
            # — the direct attr is silently overwritten here. Declare overrides
            # in a subclass body (no spec) instead; those are left untouched.
            if not isinstance(spec, AgentSpec):
                raise TypeError(
                    f"{cls.__name__}.spec must be an AgentSpec, got {type(spec).__name__}"
                )
            cls.domain = spec.domain
            cls.schedule = spec.schedule
            cls.skip_hook = spec.skip_hook
            cls.dispatchable = spec.dispatchable

    # B6: declarative agent metadata (Finding 5). Previously wiring a new
    # scheduled collector required 3 separate hand-edits (supervisor.py's
    # AGENT_REGISTRY dict, its SKIP_HOOK set, and scheduler.py's
    # _DEFAULT_SCHEDULES dict) with the exception categories only explained in
    # comments — a documented past source of a real bug (the "query" dead
    # dispatch slot: it was registered but its scoped scheduling requirement
    # was easy to miss because nothing forced the schedule to be declared
    # alongside the class). These class attributes are now the single source
    # of truth: supervisor.py builds AGENT_REGISTRY/SKIP_HOOK and scheduler.py
    # builds its default schedule table from one iteration over the agent
    # classes, reading these attributes, instead of maintaining 3 separate
    # structures by hand. A subclass MUST set `schedule` explicitly — either a
    # 5-field cron string or `None` (documented as "no default schedule" —
    # e.g. hook-only agents, or on-demand-only agents) — see
    # tests/agents/test_agent_registry_metadata.py's
    # test_every_registered_agent_declares_a_schedule for the CI guard.
    schedule: str | None = None
    # skip_hook=True: this domain must NOT re-trigger
    # supervisor.py's _post_collection_hook() after its own dispatch() call —
    # either because it IS run BY that hook (drift, notification — no
    # recursion) or because it's an on-demand/analysis agent whose dispatch is
    # not itself a "collection" the hook should react to.
    skip_hook: bool = False
    # dispatchable=False: excluded from supervisor.AGENT_REGISTRY entirely
    # (e.g. a class that exists but must never be reachable by domain-key
    # dispatch). Every currently-registered agent is dispatchable=True
    # (the default) — this exists for forward-compatibility, not because any
    # current agent needs it.
    dispatchable: bool = True

    def __init__(self):
        self.settings = get_settings()
        self.callbacks = build_callbacks(
            agent_name=type(self).__name__,
            domain=self.domain,
        )
        # Default until run() assigns the real run_id (AA-R-16 below). Lets
        # any code path that reads self._active_run_id before/without a
        # run() call (direct unit tests of collect-phase helpers, etc.) get
        # None instead of an AttributeError.
        self._active_run_id: uuid.UUID | None = None

    @abstractmethod
    def collect(self, scope: str) -> "list[dict] | CollectOutcome":
        """Return resource dicts {name, type, data} or a CollectOutcome.

        Returning a plain list is treated as an ok (no-error) outcome.
        Collectors that can partially fail MUST return CollectOutcome with
        every swallowed failure recorded in .errors (F-007). Raise
        CollectorSkipped for an intentional no-op.
        """

    def _call_with_timeout(self, fn, *args, **kwargs):
        """Run *fn* under the collect wall-clock timeout (the F-004.4 guard).

        Shared by run() (for collect()) and by run()-overriding agents
        (host_reconcile, drift, netdiscovery) so no code path loses the guard.
        Raises RuntimeError on timeout; the timed-out thread finishes in the
        background (shutdown(wait=False) never blocks).

        TRK-117: an agent may declare a per-domain override via
        ``AgentSpec.collect_timeout_seconds`` (graph_maintenance's full pass
        legitimately runs longer than the global default). Resolve it through
        the shared ``collect_timeout_for_domain`` helper so the Redis dedup
        lock TTL (dedup.default_ttl_seconds) stays in lockstep — see gotcha #2
        in that helper's docstring.
        """
        spec = getattr(type(self), "spec", None)
        override = getattr(spec, "collect_timeout_seconds", None) if spec is not None else None
        timeout = override if override is not None else get_settings().collect_timeout_seconds
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout:
            name = getattr(fn, "__name__", "collect")
            raise RuntimeError(f"{name}() timed out after {timeout}s") from None
        finally:
            pool.shutdown(wait=False)  # never block; timed-out threads run to completion in bg

    # --- Shared run()-override hardening (M-1) -----------------------------
    #
    # ETLConnector.run() below carries two safety properties that a subclass
    # writing its OWN run() silently loses: the SEC-2 scrub_dsn() on every
    # string that reaches CollectionRun.error_message, and the TRK-106
    # finalize-in-a-finally that survives a BaseException. Four agents
    # (drift, host_reconcile, netdiscovery, discovery) override run(); three
    # of them had lost both. Rather than copy the hardening into each
    # override, it lives here as two helpers that overrides call — one place
    # to fix, and one place a future override has to reach for.
    #
    # tests/agents/test_run_override_hardening.py enforces the invariant
    # behaviourally for EVERY class whose run() differs from this one,
    # discovered programmatically — a fifth override cannot reintroduce the
    # defect unnoticed.

    def _finalize_run(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        error_message: str | None = None,
        **columns,
    ) -> None:
        """Stamp a terminal ``status`` + ``finished_at`` on this run's row.

        SEC-2: ``error_message`` ALWAYS passes through :func:`scrub_dsn` here,
        so a ``run()`` override cannot persist DSN credentials by forgetting
        the call — the scrub is a property of the write path, not of each call
        site's discipline. Extra ``columns`` (``resources_found``,
        ``detail_rows_written``, ``drift_count``, ...) are applied by name.

        Never raises: a DB blip while finalizing must not mask the original
        failure the caller is in the middle of recording. ``run_row_guard``
        is the backstop that catches a row left ``in_progress`` by such a
        blip, and the hourly stale-run reaper is the backstop for that.
        """
        try:
            with get_session() as session:
                run = session.get(CollectionRun, run_id)
                if run is not None:
                    run.finished_at = datetime.now(timezone.utc)
                    run.status = status
                    if error_message is not None:
                        run.error_message = scrub_dsn(error_message)
                    for column, value in columns.items():
                        setattr(run, column, value)
                session.commit()
        except Exception:
            logger.exception(
                "%s: failed to finalize CollectionRun for domain=%s (run_id=%s) — "
                "stale-run reaper is the backstop",
                type(self).__name__,
                self.domain,
                run_id,
                extra={"domain": self.domain, "run_id": str(run_id)},
            )

    @contextmanager
    def run_row_guard(self, run_id: uuid.UUID, *, fallback_status: str = "failed"):
        """TRK-106 for ``run()`` overrides: the row is finalized on EVERY exit path.

        Wrap the whole body of a ``run()`` override in this. The ``finally``
        re-reads the row and, if it is still ``in_progress``, stamps a terminal
        status + ``finished_at``. That covers the exit paths an override's
        ``except Exception`` cannot: a ``BaseException`` such as
        ``KeyboardInterrupt``/``SystemExit`` (a scheduler shutdown mid-collect),
        and an override that simply forgot a finalize on some branch. Without
        it the row is stranded ``in_progress``/``finished_at=NULL`` forever and
        only the hourly stale-run reaper eventually notices.

        Deliberately reads the CURRENT row state rather than tracking whether
        the body called ``_finalize_run`` — DB truth needs no bookkeeping and
        stays correct if the override finalizes on a path this class never
        sees. A bare ``finally`` re-raises the in-flight exception
        automatically after the block runs; nothing here swallows it.
        """
        try:
            yield
        finally:
            try:
                with get_session() as session:
                    run = session.get(CollectionRun, run_id)
                    if run is not None and run.status == "in_progress":
                        run.status = fallback_status
                        run.finished_at = datetime.now(timezone.utc)
                        if run.error_message is None:
                            run.error_message = (
                                f"{type(self).__name__}.run() exited without finalizing this "
                                "run (interrupted, or an unhandled non-Exception exit)"
                            )
                    session.commit()
            except Exception:
                logger.exception(
                    "%s: run_row_guard failed to finalize CollectionRun for domain=%s "
                    "(run_id=%s) — stale-run reaper is the backstop",
                    type(self).__name__,
                    self.domain,
                    run_id,
                    extra={"domain": self.domain, "run_id": str(run_id)},
                )

    def run(
        self,
        trigger_type: str = "scheduled",
        scope: str = "all",
        sweep_id: uuid.UUID | None = None,
    ) -> CollectionResult:
        run_id = uuid.uuid4()
        # AA-R-16: expose the freshly-created run_id on the instance BEFORE
        # collect() is invoked, so a subclass's collect() (or an override that
        # still funnels through this method) can read the *real* run_id
        # directly instead of inferring it via a "newest in_progress" DB query
        # — the fragile pattern DiscoveryAgent used to use, which a race
        # between two overlapping runs for the same domain could pick the
        # wrong row for. Each dispatch() call constructs a fresh agent
        # instance (see supervisor.dispatch), so this attribute is never
        # shared across concurrent runs of the same domain.
        self._active_run_id = run_id
        with get_session() as session:
            run = CollectionRun(
                id=run_id,
                domain=self.domain,
                trigger_type=trigger_type,
                trigger_source=scope,
                status="in_progress",
                sweep_id=sweep_id,
            )
            session.add(run)
            session.commit()

        # TRK-106: the CollectionRun row is finalized in a SINGLE finally block
        # below so that EVERY exit path — success, CollectorSkipped, a generic
        # Exception, AND a BaseException such as KeyboardInterrupt/SystemExit
        # (which `except Exception` does NOT catch) — writes a terminal status
        # + finished_at. The previous three duplicated finalize blocks (one per
        # except branch) could miss an exit path, leaving the row stuck
        # status="in_progress"/finished_at=NULL forever. `final_status` starts
        # pessimistic ("failed") so an unexpected early exit records failed
        # rather than leaving in_progress.
        resources_found = 0
        errors = []
        final_status = "failed"
        error_message = None

        try:
            # F-022: honour the collection_disabled_domains knob BEFORE calling
            # collect() so a maintenance pause does not need code changes.
            _disabled = {
                d.strip()
                for d in (self.settings.collection_disabled_domains or "").split(",")
                if d.strip()
            }
            if self.domain in _disabled:
                raise CollectorSkipped(f"domain '{self.domain}' is in collection_disabled_domains")
            raw = self._call_with_timeout(self.collect, scope)
            outcome = raw if isinstance(raw, CollectOutcome) else CollectOutcome(items=raw)
            errors.extend(scrub_dsn(e) for e in outcome.errors)
            # A mid-loop exception (e.g. a malformed item raising KeyError, or a
            # DB constraint violation) rolls back this ENTIRE with-block via
            # get_session()'s plain `with Session(engine) as session:` — every
            # item processed earlier in this same batch is discarded along with
            # the failing one, since no commit() is reached. Accumulate into a
            # local counter first, and only publish it to the outer
            # `resources_found` (which the `finally` below persists onto the
            # CollectionRun row) AFTER session.commit() actually succeeds. If
            # the loop or the commit raises, `resources_found` is never
            # reassigned here and keeps its pessimistic initial value of 0 —
            # matching the fact that nothing from this batch survived the
            # rollback, instead of persisting a nonzero count on a "failed" run
            # that has no corresponding DB rows.
            batch_resources_found = 0
            with get_session() as session:
                for item in outcome.items:
                    resource = self._upsert_resource(session, item)
                    self._write_snapshot(session, resource.id, run_id, item.get("data", {}))
                    batch_resources_found += 1
                session.commit()
            resources_found = batch_resources_found
            if outcome.count_override is not None:
                resources_found = outcome.count_override

            # R3 status mapping: ok->completed, partial->partial, failed->failed.
            if outcome.status == "failed":
                final_status = "failed"
            elif outcome.status == "partial":
                final_status = "partial"
            else:
                final_status = "completed"
            if errors:
                error_message = _join_errors_truncated(errors)

        except CollectorSkipped as exc:
            # Intentional no-op: dependency unconfigured.  Record "skipped" so
            # collection-health monitors can distinguish this from a working-empty
            # run ("completed", resources_found=0) and a runtime failure ("failed").
            reason = str(exc) or "unconfigured"
            logger.info(
                "BaseAgent.run skipped for domain=%s reason=%r",
                self.domain,
                reason,
                extra={"domain": self.domain, "run_id": str(run_id)},
            )
            final_status = "skipped"
            error_message = reason

        except Exception as exc:
            logger.exception(
                "BaseAgent.run failed for domain=%s",
                self.domain,
                extra={"domain": self.domain, "run_id": str(run_id)},
            )
            scrubbed = scrub_dsn(str(exc))
            errors.append(scrubbed)
            final_status = "failed"
            error_message = scrubbed

        finally:
            # TRK-106: the SINGLE authoritative finalize. A bare finally re-raises
            # any in-flight exception (including BaseException) automatically AFTER
            # this block runs, so the row is always finalized even on
            # KeyboardInterrupt/SystemExit. The inner try/except must NOT swallow
            # that in-flight exception — it only logs a failure of the finalize
            # commit ITSELF (a DB blip) rather than letting it propagate and mask
            # the original error; the hourly stale-run reaper is the backstop for
            # that residual case. Never `return` inside this finally (a return
            # would suppress the propagating exception).
            try:
                with get_session() as session:
                    run = session.get(CollectionRun, run_id)
                    if run is not None:
                        run.finished_at = datetime.now(timezone.utc)
                        run.status = final_status
                        run.resources_found = resources_found
                        if error_message is not None:
                            run.error_message = error_message
                    session.commit()
            except Exception:
                logger.exception(
                    "BaseAgent.run failed to finalize CollectionRun for domain=%s "
                    "(run_id=%s) — stale-run reaper is the backstop",
                    self.domain,
                    run_id,
                    extra={"domain": self.domain, "run_id": str(run_id)},
                )

        # A failure counting drift events must not raise AFTER the row is already
        # correctly finalized above — fall back to 0.
        try:
            with get_session() as session:
                drift_count = count_drift_events_for_run(run_id, session)
        except Exception:
            logger.exception(
                "BaseAgent.run failed to count drift events for domain=%s (run_id=%s)",
                self.domain,
                run_id,
                extra={"domain": self.domain, "run_id": str(run_id)},
            )
            drift_count = 0

        result = CollectionResult(
            run_id=run_id,
            domain=self.domain,
            resources_found=resources_found,
            drift_count=drift_count,
            status=final_status,
            errors=errors,
            scope=scope,
        )

        # TRK-061/062: the detail-write phase used to live in a per-collector
        # run() override that wrapped super().run() with an identical
        # ``result = super().run(...); self._write_details(result, fn); return
        # result`` skeleton, duplicated across ~10 collectors. It is now driven
        # here from the base run() so that boilerplate is not repeated. Each
        # writer is executed through _write_details so a detail-write failure is
        # surfaced on the CollectionRun/result exactly as before (never silently
        # swallowed). The base default _detail_writers() is empty -> no-op, so a
        # collector that overrides neither hook is unaffected.
        for _writer in self._detail_writers(scope, result):
            self._write_details(result, _writer)

        # linux/windows write DriftEvent rows in their detail phase, i.e. AFTER
        # drift_count was computed above -> recompute so the returned result is
        # not stale. Mirrors the old per-collector recompute tail exactly (no
        # extra guard: a failure here propagated before this refactor too).
        #
        # 2026-08-11: this used to update ONLY the in-memory result and leave the
        # PERSISTED CollectionRun.drift_count at its pre-detail value — which for
        # these two collectors is always 0, because they write every one of their
        # DriftEvent rows in the detail phase. So the run row said "0 drift" while
        # drift_events held the real count, forever. Found by verifying a live
        # post-deploy linux run: it returned drift_count=14 and wrote 14
        # drift_events, and its collection_runs row said 0. Historic rows show the
        # same divergence (2026-08-02 12:00 UTC: column 0, actual 6).
        #
        # That column is what run-list readers show, so linux/windows drift was
        # invisible there. Persist the recomputed value in the same session that
        # computes it — the returned object and the stored row must not disagree
        # about what the run found.
        if self._recompute_drift_after_details:
            with get_session() as session:
                recount = count_drift_events_for_run(run_id, session)
                result.drift_count = recount
                row = session.get(CollectionRun, run_id)
                if row is not None:
                    row.drift_count = recount
                    session.commit()

        return result

    # TRK-061/062: subclasses that write DriftEvent rows in their detail phase
    # (linux, windows) set this True so run() recomputes drift_count AFTER the
    # detail writers run. Default False -> no recompute.
    _recompute_drift_after_details: ClassVar[bool] = False

    def _detail_writers(
        self, scope: str, result: "CollectionResult"
    ) -> Iterable[Callable[[], object]]:
        """Ordered detail-write phases for this collector.

        Each callable takes no arguments and may return an int row-count or
        None; base ``run()`` executes each via ``self._write_details`` after the
        generic Resource/Snapshot collect, so a failure in any phase is surfaced
        on the CollectionRun (never silently swallowed). Order is preserved, so a
        phase that depends on an earlier phase's side effects (e.g. vuln's slug
        harvest) can rely on it.

        Default: no detail writers. Relational collectors override THIS instead
        of overriding ``run()`` (TRK-061/062 collapse of the duplicated run()
        wrapper).
        """
        return ()

    def _upsert_resource(self, session, item: dict) -> Resource:
        """Thin adapter over the canonical ``api._seeding.upsert_resource``.

        Task 4.4: this used to be a standalone hand-rolled upsert that OVERWROTE
        ``metadata_`` on update. It now delegates so every ingress (HTTP, MCP,
        collectors) shares one write path — metadata_ is MERGED, not overwritten,
        on update. Collectors write stable key sets each cycle so merge stays
        idempotent in practice.
        """
        from infra_brain.api._seeding import upsert_resource  # noqa: PLC0415

        return upsert_resource(
            session,
            name=item["name"],
            domain=self.domain,
            resource_type=item.get("type", "unknown"),
            metadata=item.get("data", {}),
            zone=self.settings.default_zone,
            source=type(self).__name__,
        )

    def _write_snapshot(self, session, resource_id: uuid.UUID, run_id: uuid.UUID, data: dict):
        session.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource_id,
                run_id=run_id,
                snapshot=data,
            )
        )

    # --- Reusable detail-table write helpers -------------------------------
    #
    # Rich relational collectors (octopus, iac/cicd, vsphere, ...) write
    # normalized detail rows in a phase that runs AFTER the generic Resource
    # collect. Two pitfalls keep recurring, so the pattern is centralized here:
    #   1. Every collector re-implemented its own natural-key upsert.
    #   2. A failure in the detail-write phase was only logged, so dispatch
    #      reported status="completed"/errors=[] while the detail tables stayed
    #      empty or partial — an invisible data-loss bug (the Octopus bug).
    # ``_upsert_detail`` solves (1); ``_write_details`` solves (2) by mirroring
    # exactly how ``run()`` surfaces a collect failure.

    def _resource_id(
        self,
        session,
        type_: str,
        name: str,
        *,
        qualify: Callable[[str], str] | None = None,
    ) -> uuid.UUID | None:
        """Resolve the Resource.id for a detail row by ``(self.domain, type_, name)``.

        Task 4: consolidates three near-identical copies (k8s, octopus,
        vsphere). None-guard on empty/missing ``name`` (previously
        octopus-only) is now universal — a caller should never fire a query
        for a detail row that has no name to key on. ``qualify`` lets a
        collector transform the bare name before the lookup (vsphere passes
        ``lambda n: self._qualified_name(session, n, vcenter, item_type, moref)``
        since its canonical Resource rows are vCenter-qualified, and
        collision-disambiguated by moref when two objects share a name).
        """
        if not name:
            return None
        lookup_name = qualify(name) if qualify is not None else name
        res = (
            session.query(Resource)
            .filter_by(domain=self.domain, type=type_, name=lookup_name)
            .first()
        )
        return res.id if res else None

    def _write_each(
        self,
        session,
        items: Iterable,
        write_fn: Callable,
        label_fn: Callable[[object, Exception], str] | None = None,
    ) -> tuple[int, int]:
        """Write each item in its own SAVEPOINT; warn and continue on failure.

        Task 4: consolidates the per-item ``session.begin_nested()`` pattern
        (DL-C-5/AA-R-14) that recurred across cloud/iac/vuln/octopus
        detail-writers — a bad row must only roll back that row, never abort
        every remaining item in the batch (the failure mode a single shared
        try/except with no SAVEPOINT produces).

        ``write_fn(item)`` performs the write for one item; the caller owns
        what "one item" means (a raw collected dict, a pre-built row, etc.).
        On success the SAVEPOINT is released and ``written`` increments. On
        any exception the SAVEPOINT rolls back (leaving prior successful
        items intact) and ``skipped`` increments.

        ``label_fn(item, exc)`` — when given — must return the FULL warning
        message to log (so each call site preserves its own exact log
        wording); when omitted a generic message is logged instead.

        Returns ``(written, skipped)``.
        """
        written = 0
        skipped = 0
        for item in items:
            try:
                with session.begin_nested():
                    write_fn(item)
                written += 1
            except Exception as exc:
                skipped += 1
                if label_fn is not None:
                    logger.warning(label_fn(item, exc))
                else:
                    logger.warning("%s: skipping item %r: %s", type(self).__name__, item, exc)
        return written, skipped

    def _upsert_detail(self, session, model, row: dict, key_fields: list[str]) -> None:
        """Upsert one detail row keyed on its natural key.

        Queries ``model`` for an existing row matching
        ``{k: row[k] for k in key_fields}``. If found, updates every column in
        ``row`` on that row in place; otherwise ``session.add(model(**row))``.
        The caller owns the transaction and must commit.
        """
        criteria = {k: row[k] for k in key_fields}
        existing = session.query(model).filter_by(**criteria).first()
        if existing is not None:
            for column, value in row.items():
                setattr(existing, column, value)
        else:
            session.add(model(**row))

    def _record_partial_errors(self, result: "CollectionResult | None", errors: list[str]) -> None:
        """Surface non-fatal per-unit failures from a DETAIL-write phase as "partial".

        Companion to :class:`ReconcileScope` for collectors whose per-unit fan-out
        happens in a ``_detail_writers()`` phase rather than in ``collect()``. A
        collect-phase collector needs nothing extra — it returns
        ``CollectOutcome(items=..., errors=scope.errors)`` and ``run()``'s existing
        R3 status mapping turns that into ``"partial"``. A detail-phase collector
        has already had its status computed by the time the phase runs, so the
        downgrade is applied here instead — writing the SAME
        ``CollectionRun.status`` / ``error_message`` fields, with the SAME
        ``"partial"`` literal ``CollectOutcome.status`` produces. This is not a
        second signalling channel; it is the same one, applied later.

        Only ever downgrades ``"completed"`` -> ``"partial"``. An already
        ``"failed"`` run (e.g. an earlier detail phase raised, or the collect
        phase itself failed) is left alone — a swallowed per-unit failure must
        never soften a hard failure.

        ``result=None`` (a helper invoked outside a run, e.g. a direct unit test
        of the phase) logs and returns: the destructive-pass narrowing that
        :class:`ReconcileScope` provides is the safety-critical half and does not
        depend on this reporting half.
        """
        if not errors:
            return
        scrubbed = [scrub_dsn(e) for e in errors]
        if result is None:
            logger.warning(
                "%s: %d unit fetch failure(s) with no CollectionResult to report on: %s",
                type(self).__name__,
                len(scrubbed),
                _join_errors_truncated(scrubbed),
            )
            return

        result.errors.extend(scrubbed)
        if result.status == "completed":
            result.status = "partial"
        try:
            with get_session() as session:
                run = session.get(CollectionRun, result.run_id)
                if run is not None:
                    if run.status == "completed":
                        run.status = "partial"
                    run.error_message = _join_errors_truncated(result.errors)
                session.commit()
        except Exception:
            logger.exception(
                "%s: failed to record partial-collection errors on CollectionRun",
                type(self).__name__,
            )

    def _write_details(self, result: CollectionResult, fn) -> None:
        """Run the detail-write phase ``fn`` so a failure is SURFACED, not swallowed.

        Calls ``fn()`` (the collector's own detail-table writer). The function
        may return an int (detail row count) or None; if it returns an int,
        the count is accumulated in ``result.detail_rows_written`` and
        persisted to ``CollectionRun.detail_rows_written`` so collection-health
        monitors can confirm that detail-only collectors (those that write no
        generic Resource rows) actually produced data.

        On any exception the failure is recorded the same way ``run()`` records a
        collect failure — ``status="failed"`` + ``error_message`` on the matching
        ``CollectionRun`` row, and the message appended to ``result.errors`` (which
        also flips ``result.status`` to ``"failed"``) — then logged. A
        detail-write failure must show up on the run status; it must never report
        "completed" silently. The generic Resource collect already committed, so
        the guard keeps a detail failure from rolling that back.
        """
        try:
            detail_count = fn()
            if isinstance(detail_count, int) and detail_count > 0:
                result.detail_rows_written += detail_count
                try:
                    with get_session() as session:
                        run = session.get(CollectionRun, result.run_id)
                        if run is not None:
                            run.detail_rows_written = result.detail_rows_written
                        session.commit()
                except Exception:
                    logger.exception(
                        "%s: failed to persist detail_rows_written on CollectionRun",
                        type(self).__name__,
                    )
        except Exception as exc:
            logger.exception("%s detail-table write failed", type(self).__name__)
            msg = scrub_dsn(f"detail-table write failed: {exc}")
            result.errors.append(msg)
            result.status = "failed"
            try:
                with get_session() as session:
                    run = session.get(CollectionRun, result.run_id)
                    if run is not None:
                        run.status = "failed"
                        # F-… (this fix): accumulate across every _write_details
                        # call for this run, not just the most recent one — a
                        # multi-phase _detail_writers() (octopus: 2 phases,
                        # vuln: 5) can fail more than once for distinct root
                        # causes, and a plain overwrite here silently dropped
                        # every failure but the last from the DB-persisted
                        # error_message even though result.errors kept all of
                        # them. Mirrors the collect-phase's own
                        # _join_errors_truncated handling for consistency.
                        run.error_message = _join_errors_truncated(result.errors)
                    session.commit()
            except Exception:
                logger.exception(
                    "%s: failed to record detail-write error on CollectionRun",
                    type(self).__name__,
                )
