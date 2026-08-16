from infra_brain.agents.base import BaseAgent
from infra_brain.callbacks.registry import build_callbacks
from infra_brain.config import get_settings  # noqa: F401  # re-exported; tests patch this
from infra_brain.db.models import AuditLog, ComplianceViolation, DriftEvent, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.confluence import build_domain_page_body, upsert_confluence_page
from infra_brain.tools.jira import create_compliance_jira_ticket, create_jira_ticket
from infra_brain.tools.ops_webhook import send_ops_alert
from infra_brain.tools.webhook_publish import publish_event


class NotificationAgent(BaseAgent):
    spec = AgentSpec(
        domain="notification",
        tier=Tier.REASONER,
        schedule=None,
        max_staleness=None,
        skip_hook=True,
    )

    def __init__(self):
        super().__init__()
        # Override callbacks to whitelist POST to Jira + Confluence + the
        # ops-alert webhook (OB-1) only.
        self.callbacks = build_callbacks(
            agent_name="NotificationAgent",
            domain="notification",
            whitelisted_post=[
                url
                for url in (
                    self.settings.jira_url,
                    self.settings.confluence_url,
                    self.settings.ops_webhook_url,
                )
                if url
            ],
        )

    def collect(self, scope: str = "all") -> list[dict]:
        return []

    @staticmethod
    def _record_skip(session, *, tool: str, reason: str) -> None:
        """Persist an audit-trail row when a notification sink is skipped
        because it isn't configured (TRK-277 / GitLab #157).

        Without this, a sweep where e.g. Jira is unconfigured is
        indistinguishable from a sweep that legitimately had nothing to do —
        both look like silence. This gives a health check or dashboard
        something to query: ``get_audit_log(agent="NotificationAgent",
        allowed=False)`` surfaces "sweep ran, delivery skipped: <reason>"
        rows, one per skipped sink per sweep.
        """
        session.add(
            AuditLog(
                agent="NotificationAgent",
                tool=tool,
                input_hash="",
                allowed=False,
                denial_reason=reason,
            )
        )
        session.commit()

    @staticmethod
    def _record_truncation(session, *, tool: str, total: int, cap: int) -> None:
        """M-4: persist an audit-trail WARNING row when one sweep hits
        ``notification_max_per_sweep`` and cannot process every open row.

        Without this, capping the per-sweep fetch would silently notify
        LESS than the true backlog with no visible signal — indistinguishable
        from "everything got processed". The remainder is left in its
        current status (untouched, so nothing is lost) and picked up by the
        next sweep; this row makes that fact queryable
        (``get_audit_log(agent="NotificationAgent", allowed=False)``), same
        as the sibling "sink not configured" skip rows above.
        """
        session.add(
            AuditLog(
                agent="NotificationAgent",
                tool=tool,
                input_hash="",
                allowed=False,
                denial_reason=(
                    f"notification_max_per_sweep={cap} reached; {total} open row(s) "
                    f"found, only {cap} processed this sweep, "
                    f"{total - cap} deferred to a later sweep"
                ),
            )
        )
        session.commit()

    def notify_ops_alerts(self, category: str, messages: list[str]) -> bool:
        """Deliver a batch of ops alerts (OB-1) to the configured webhook.

        Used by the scheduler for collection-health alerts, the scheduler
        dead-man switch, and agent-anomaly alerts (deny-verdict spikes,
        runaway recursion loops). Every alert is already logged at ERROR
        level by the caller before this runs — this is best-effort delivery
        on top of that, not the only signal.

        TRK-277 / GitLab #157: when the ops webhook is unconfigured,
        ``send_ops_alert`` itself returns True (a "clean no-op" contract this
        call preserves) — but that made a skipped delivery indistinguishable
        from a delivered one to any caller only looking at the return value.
        Record an audit-trail row for the unconfigured case so that
        distinction is queryable instead of silent.
        """
        import logging as _log

        _logger = _log.getLogger(__name__)
        ops_enabled = bool((self.settings.ops_webhook_url or "").strip())
        if not ops_enabled:
            _logger.info(
                "[notification] ops webhook not configured; skipping delivery of "
                "%d %s alert(s)",
                len(messages),
                category,
            )
            try:
                with get_session() as session:
                    self._record_skip(
                        session,
                        tool="notify_ops_alerts",
                        reason=(
                            f"ops_webhook_url not configured; category={category}, "
                            f"{len(messages)} alert(s) not delivered"
                        ),
                    )
            except Exception:
                _logger.error(
                    "[notification] failed to record ops-webhook skip audit row for "
                    "category=%s",
                    category,
                    exc_info=True,
                )
            delivered = True
        else:
            try:
                delivered = send_ops_alert(category, messages, agent_name="NotificationAgent")
                if not delivered:
                    _logger.error(
                        "[notification] ops-webhook delivery failed for category=%s (%d alert(s))",
                        category,
                        len(messages),
                    )
            except Exception:
                _logger.error(
                    "[notification] notify_ops_alerts raised for category=%s",
                    category,
                    exc_info=True,
                )
                delivered = False

        # Issue #112: also fan the same event out to every third-party
        # WebhookSubscription whose event_pattern matches this category —
        # independent of (and never blocking) the single legacy
        # ops_webhook_url delivery above. Best-effort: never let a
        # subscriber-fan-out failure change this method's return value.
        try:
            publish_event(category, messages, agent_name="NotificationAgent")
        except Exception:
            _logger.error(
                "[notification] publish_event raised for category=%s", category, exc_info=True
            )

        return delivered

    def notify_incidents(self) -> int:
        """Correlate open DriftEvents/ComplianceViolations into incident-management
        alerts (TRK-242 / GitLab #113).

        Sends one ops-webhook alert per not-yet-correlated open row, with a
        stable ``dedup_key`` derived from the thing that repeats (resource +
        field for drift, rule + host for compliance) so PagerDuty/Opsgenie
        group repeat alerts into a single incident instead of paging on
        every sweep. On successful delivery, persists that dedup_key onto
        the row's ``incident_key`` so the ack-in webhook
        (POST /webhooks/incident/ack) can later find it.

        When the ops webhook is unconfigured, correlation cannot safely
        proceed — see ``send_ops_alert``'s "unconfigured == True" contract:
        running the loop unchanged would set ``incident_key`` on rows that
        were never actually delivered anywhere. So this still short-circuits
        before the loop (TRK-277 / GitLab #157), but now records an
        audit-trail row instead of silently returning 0, so a health check
        can tell "ops webhook off" apart from "ops webhook on, nothing new to
        correlate this cycle."
        """
        import logging as _log

        _logger = _log.getLogger(__name__)

        # NOTE: this early-return is keyed on the legacy single
        # ops_webhook_url only (unchanged from before issue #112) to avoid
        # an extra DB round-trip on every call — third-party
        # WebhookSubscriptions still get a fan-out attempt inside the loop
        # below via publish_event(), which is its own clean no-op when there
        # are no active matching subscriptions. A stack running on
        # subscriptions alone (no legacy webhook configured) can flip this
        # gate by setting OPS_WEBHOOK_URL to a placeholder, or the gate can
        # be revisited if that combination becomes a real deployment shape.
        ops_enabled = bool((self.settings.ops_webhook_url or "").strip())
        if not ops_enabled:
            _logger.info("[notification] ops webhook not configured; skipping incident correlation")
            with get_session() as session:
                self._record_skip(
                    session,
                    tool="notify_incidents:ops_webhook",
                    reason="ops_webhook_url not configured; incident correlation skipped",
                )
            return 0

        correlated = 0
        with get_session() as session:
            open_events = (
                session.query(DriftEvent)
                .filter_by(status="open")
                .filter(DriftEvent.incident_key.is_(None))
                .all()
            )
            for event in open_events:
                resource = session.get(Resource, event.resource_id)
                if not resource:
                    continue
                dedup_key = f"drift:{event.resource_id}:{event.field}"
                message = (
                    f"[Infra Drift] {resource.domain}/{resource.name}: "
                    f"{event.drift_type} on '{event.field}'"
                )
                try:
                    delivered = send_ops_alert(
                        "drift_incident",
                        [message],
                        agent_name="NotificationAgent",
                        dedup_key=dedup_key,
                    )
                    try:
                        publish_event(
                            "drift.incident",
                            [message],
                            domain=resource.domain,
                            dedup_key=dedup_key,
                            agent_name="NotificationAgent",
                        )
                    except Exception:
                        _logger.error(
                            "[notification] publish_event raised for drift event %s",
                            event.id,
                            exc_info=True,
                        )
                    if delivered:
                        event.incident_key = dedup_key
                        session.commit()
                        correlated += 1
                except Exception as exc:
                    _logger.error(
                        "[notification] Failed to raise incident for drift event %s: %s",
                        event.id,
                        exc,
                        exc_info=True,
                    )

            open_violations = (
                session.query(ComplianceViolation)
                .filter_by(status="open")
                .filter(ComplianceViolation.incident_key.is_(None))
                .all()
            )
            for violation in open_violations:
                dedup_key = f"compliance:{violation.rule}:{violation.host}"
                message = f"[Compliance] {violation.rule} on {violation.host}: {violation.detail}"
                try:
                    delivered = send_ops_alert(
                        "compliance_incident",
                        [message],
                        agent_name="NotificationAgent",
                        dedup_key=dedup_key,
                    )
                    violation_resource = (
                        session.get(Resource, violation.resource_id)
                        if violation.resource_id
                        else None
                    )
                    try:
                        publish_event(
                            "compliance.incident",
                            [message],
                            domain=violation_resource.domain if violation_resource else None,
                            dedup_key=dedup_key,
                            agent_name="NotificationAgent",
                        )
                    except Exception:
                        _logger.error(
                            "[notification] publish_event raised for compliance violation %s",
                            violation.id,
                            exc_info=True,
                        )
                    if delivered:
                        violation.incident_key = dedup_key
                        session.commit()
                        correlated += 1
                except Exception as exc:
                    _logger.error(
                        "[notification] Failed to raise incident for compliance violation %s: %s",
                        violation.id,
                        exc,
                        exc_info=True,
                    )

        return correlated

    def notify_all(self):
        """Run the full notification sweep: Jira ticketing for open
        DriftEvents/ComplianceViolations, then a Confluence page upsert per
        domain that got acked.

        TRK-277 / GitLab #157: this used to hard-return before even opening a
        session when ``JIRA_URL`` was unset, which skipped the ENTIRE sweep —
        not just ticket creation, but also compliance ticketing and all
        status bookkeeping — making an unconfigured no-op cycle
        indistinguishable from a working one. Each sink (Jira-for-drift,
        Jira-for-compliance, Confluence) is now gated independently, still
        runs the sweep, and records an audit-trail row when it skips so a
        health check or dashboard can query "sweep ran, sink X skipped: not
        configured" instead of seeing silence.
        """
        import logging as _log

        _logger = _log.getLogger(__name__)

        # On stacks where JIRA_URL / CONFLUENCE_URL are unset, attempting
        # these calls raises httpx.UnsupportedProtocol (caught per-event
        # below, but it spams error logs and pointlessly attempts). Skip the
        # relevant sink cleanly when unconfigured; each re-enables itself if
        # its URL is later set.
        jira_enabled = bool((self.settings.jira_url or "").strip())
        confluence_enabled = bool((self.settings.confluence_url or "").strip())

        with get_session() as session:
            acked_domains: set[str] = set()

            # M-4: this loop issues one Jira HTTP call + one commit PER open
            # DriftEvent below -- with zero bound, a drift flood could
            # attempt tens of thousands of webhook/Jira calls in a single
            # sweep. `notification_max_per_sweep` caps how many this sweep
            # will attempt; the SQL-side `.limit()` means the uncapped
            # remainder is never even fetched into Python. Any remainder
            # stays `status="open"` (untouched, nothing lost) and is picked
            # up by the next sweep -- never silently dropped, and the
            # truncation itself is recorded below, never silent.
            cap = self.settings.notification_max_per_sweep
            total_open_events = session.query(DriftEvent).filter_by(status="open").count()
            open_events = (
                session.query(DriftEvent)
                .filter_by(status="open")
                .order_by(DriftEvent.detected_at.asc())
                .limit(cap)
                .all()
            )
            if total_open_events > cap:
                _logger.warning(
                    "[notification] %d open drift event(s) exceed notification_max_per_sweep "
                    "(%d); processing the oldest %d this sweep, %d deferred",
                    total_open_events,
                    cap,
                    len(open_events),
                    total_open_events - cap,
                )
                self._record_truncation(
                    session, tool="notify_all:jira_drift", total=total_open_events, cap=cap
                )
            if not jira_enabled:
                _logger.info(
                    "[notification] Jira not configured; skipping ticketing for "
                    "%d open drift event(s)",
                    total_open_events,
                )
                self._record_skip(
                    session,
                    tool="notify_all:jira_drift",
                    reason=(
                        f"jira_url not configured; {total_open_events} open drift "
                        "event(s) not ticketed"
                    ),
                )
            else:
                for event in open_events:
                    resource = session.get(Resource, event.resource_id)
                    if not resource:
                        continue

                    summary = (
                        f"[Infra Drift] {resource.domain}/{resource.name}: "
                        f"{event.drift_type} on '{event.field}'"
                    )
                    description = (
                        f"Resource: {resource.name} ({resource.domain})\n"
                        f"Drift type: {event.drift_type}\n"
                        f"Field: {event.field}\n"
                        f"Old value: {event.old_value}\n"
                        f"New value: {event.new_value}\n"
                        f"Detected: {event.detected_at}"
                    )
                    # C5: per-event try/except so one failure does not abort the loop.
                    # Commit after each successful ack so a later failure cannot cause
                    # already-ticketed events to be re-ticketed on the next run.
                    try:
                        jira_key = create_jira_ticket(summary, description, event.id)
                        event.jira_key = jira_key
                        event.status = "acknowledged"
                        acked_domains.add(resource.domain)
                        session.commit()  # incremental commit — safe for already-acked events
                    except Exception as exc:
                        _logger.error(
                            "[notification] Failed to create Jira ticket for drift event %s: %s",
                            event.id,
                            exc,
                            exc_info=True,
                        )

            # GitLab #106: same treatment for open ComplianceViolation rows.
            # No `drift_type`-style filter here either — every open violation,
            # regardless of severity, gets a ticket, mirroring the drift path.
            # M-4: same emission cap as the drift-event loop above -- one
            # Jira HTTP call + one commit per open violation, so the fetch
            # itself is bounded and any excess is deferred + recorded, not
            # silently processed-fewer.
            total_open_violations = (
                session.query(ComplianceViolation).filter_by(status="open").count()
            )
            open_violations = (
                session.query(ComplianceViolation)
                .filter_by(status="open")
                .order_by(ComplianceViolation.detected_at.asc())
                .limit(cap)
                .all()
            )
            if total_open_violations > cap:
                _logger.warning(
                    "[notification] %d open compliance violation(s) exceed "
                    "notification_max_per_sweep (%d); processing the oldest %d this "
                    "sweep, %d deferred",
                    total_open_violations,
                    cap,
                    len(open_violations),
                    total_open_violations - cap,
                )
                self._record_truncation(
                    session,
                    tool="notify_all:jira_compliance",
                    total=total_open_violations,
                    cap=cap,
                )
            if not jira_enabled:
                _logger.info(
                    "[notification] Jira not configured; skipping ticketing for "
                    "%d open compliance violation(s)",
                    total_open_violations,
                )
                self._record_skip(
                    session,
                    tool="notify_all:jira_compliance",
                    reason=(
                        f"jira_url not configured; {total_open_violations} open "
                        "compliance violation(s) not ticketed"
                    ),
                )
            else:
                for violation in open_violations:
                    resource = (
                        session.get(Resource, violation.resource_id)
                        if violation.resource_id
                        else None
                    )
                    host_label = resource.name if resource else violation.host

                    summary = f"[Compliance Violation] {violation.rule}: {host_label}"
                    description = (
                        f"Rule: {violation.rule}\n"
                        f"Severity: {violation.severity}\n"
                        f"Host: {violation.host}\n"
                        f"Detail: {violation.detail}\n"
                        f"Detected: {violation.detected_at}"
                    )
                    # Same C5 pattern as the drift loop: per-violation try/except so
                    # one failure does not abort the rest, and an incremental commit
                    # right after a successful ticket so an already-ticketed
                    # violation is never re-ticketed on a later run.
                    try:
                        create_compliance_jira_ticket(summary, description)
                        violation.status = "acknowledged"
                        if resource:
                            acked_domains.add(resource.domain)
                        session.commit()
                    except Exception as exc:
                        _logger.error(
                            "[notification] Failed to create Jira ticket for compliance "
                            "violation %s: %s",
                            violation.id,
                            exc,
                            exc_info=True,
                        )

            # Update Confluence page per successfully-acked domain.
            # Skip the loop entirely when Confluence is unconfigured.
            if not confluence_enabled:
                if acked_domains:
                    _logger.info("[notification] Confluence not configured; skipping page upserts")
                self._record_skip(
                    session,
                    tool="notify_all:confluence",
                    reason=(
                        "confluence_url not configured; "
                        f"{len(acked_domains)} domain page upsert(s) skipped"
                    ),
                )
                acked_domains = set()
            for domain in acked_domains:
                try:
                    resources = session.query(Resource).filter_by(domain=domain).all()
                    body = build_domain_page_body(
                        domain,
                        [
                            {"name": r.name, "type": r.type, "last_seen": str(r.last_seen)}
                            for r in resources
                        ],
                    )
                    upsert_confluence_page(
                        domain=domain,
                        title=f"Infrastructure: {domain.upper()}",
                        body=body,
                    )
                except Exception as exc:
                    _logger.error(
                        "[notification] Failed to upsert Confluence page for domain %s: %s",
                        domain,
                        exc,
                        exc_info=True,
                    )
