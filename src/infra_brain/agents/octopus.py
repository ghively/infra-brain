import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from infra_brain.config import get_settings
from infra_brain.db.models import (
    AgentActionLog,
    OctopusAccount,
    OctopusActionTemplate,
    OctopusChannel,
    OctopusDeployment,
    OctopusDeploymentStep,
    OctopusEnvironment,
    OctopusEvent,
    OctopusFeed,
    OctopusInterruption,
    OctopusLibraryVariableSet,
    OctopusLifecycle,
    OctopusMachine,
    OctopusMachineRole,
    OctopusProject,
    OctopusProjectGroup,
    OctopusRelease,
    OctopusTask,
    OctopusTeam,
    OctopusUser,
    OctopusVariable,
    Resource,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.masking import dlp_scrub, luhn_valid, mask_pan_shapes
from infra_brain.tools.octopus_tool import (
    octopus_accounts_tool,
    octopus_action_templates_tool,
    octopus_channels_tool,
    octopus_dashboard_tool,
    octopus_deployment_process_tool,
    octopus_environments_tool,
    octopus_events_tool,
    octopus_feeds_tool,
    octopus_interruptions_tool,
    octopus_library_variable_sets_tool,
    octopus_lifecycles_tool,
    octopus_machine_roles_tool,
    octopus_machines_tool,
    octopus_projectgroups_tool,
    octopus_projects_tool,
    octopus_releases_tool,
    octopus_server_info_tool,
    octopus_tasks_tool,
    octopus_teams_tool,
    octopus_users_tool,
    octopus_variableset_tool,
)
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.pool_metrics import observe_pool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PAN masking (DLP — F-019)
# ---------------------------------------------------------------------------
#
# Generalized into infra_brain.etl.masking (SEC/DL-C item — "mask-at-ingestion")
# so other collectors can reuse the same Luhn-check + masking logic instead of
# each re-implementing it. These thin wrappers keep the original names/call
# signatures (existing tests + call sites import them from this module) and
# preserve the "octopus DLP: ..." log line under THIS module's logger — the
# shared helper accepts an explicit ``log`` so the log namespace doesn't shift
# to infra_brain.etl.masking when the implementation moved.

_luhn_valid = luhn_valid
_mask_pan_shapes = mask_pan_shapes


def _dlp_record(record: dict, context: str = "") -> dict:
    """Apply PAN masking to a single Octopus API response *record* (dict).

    Logs a WARNING (count only, no values) when at least one field was masked.
    Returns the masked dict.  The caller replaces its local variable with the
    return value — never passing the original *record* to downstream code.
    """
    return dlp_scrub(record, context, log=log, source="octopus")


# ---------------------------------------------------------------------------


def _parse_dt(value) -> datetime | None:
    """Parse an Octopus ISO-8601 timestamp into an aware datetime, or None.

    Octopus 3.3.8 emits e.g. ``2018-04-03T19:34:12.308+00:00``. The trailing ``Z``
    form (used in tests/fixtures) is normalized to ``+00:00`` for fromisoformat.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class OctopusAgent(ETLConnector):
    """Collects the Octopus Deploy estate (pre-Spaces 3.3.8).

    ``collect()`` emits the generic ``Resource`` rows (server / project /
    environment / machine) with sensible names + minimal metadata. The rich,
    fully-resolved relational data is written by ``run()`` into the detail tables
    (octopus_project_groups, octopus_lifecycles, octopus_projects,
    octopus_environments, octopus_machines, octopus_channels, octopus_releases,
    octopus_deployments) — the LinuxAgent collect()/_write_*_details() split.

    All upstream API calls are read-only GETs. The tenants endpoint (404 on 3.3.8)
    is never touched.
    """

    spec = AgentSpec(
        domain="octopus",
        tier=Tier.COLLECTOR,
        schedule="10 2 * * *",
        max_staleness=timedelta(hours=26),
        # 2026-08-12: retired. Octopus Deploy is an enterprise remnant — there
        # is no Octopus server in this home lab and there will not be. The MCP
        # surface for it was already removed wholesale (docs/TRACKER.md rows
        # 182/183/207, "the whole Octopus surface removed, P7.1a/D6/D11"), and
        # the 2026-08-02 coverage audit lists octopus among the "deliberately
        # dormant" domains with an explicit prior decision. This is that
        # decision made structural instead of leaving the collector to skip
        # nightly. Re-enable with COLLECTION_REVIVED_DOMAINS=octopus.
        retired=True,
    )

    # --- collect: generic Resource rows only -------------------------------

    def collect(self, scope: str = "all") -> CollectOutcome:
        # Unconfigured -> self-skip, matching every sibling collector
        # (wazuh.py, vsphere.py, ...). Without this guard each tool call below
        # failed on its own with "Octopus Deploy is not configured
        # (OCTOPUS_URL unset)" and BaseAgent.run() recorded status="failed" —
        # so a collector that was simply never set up looked identical to a
        # broken one, permanently inflating the failure count and firing
        # collection-health alerts that no amount of fixing could clear.
        if not (self.settings.octopus_url or "").strip():
            raise CollectorSkipped(
                "octopus_url not configured; set OCTOPUS_URL to enable Octopus Deploy collection"
            )

        items: list[dict] = []
        errors: list[str] = []
        _cb = {"callbacks": self.callbacks}

        # Server info — store Version only (no Edition in the rich model).
        try:
            info = octopus_server_info_tool.invoke({}, config=_cb)
            self._last_server = info
            items.append(
                {
                    "name": "octopus-server",
                    "type": "octopus_server",
                    "data": {"version": info.get("Version", "")},
                }
            )
        except Exception as exc:
            log.warning("Octopus server info: %s", exc)
            errors.append(f"server info failed: {exc}")

        # Environments
        try:
            envs = octopus_environments_tool.invoke({}, config=_cb)
            self._last_envs = envs
            for env in envs:
                items.append(
                    {
                        "name": env["Name"],
                        "type": "octopus_environment",
                        "data": {"environment_id": env["Id"]},
                    }
                )
        except Exception as exc:
            log.warning("Octopus environments: %s", exc)
            errors.append(f"environments failed: {exc}")

        # Machines
        try:
            machines = octopus_machines_tool.invoke({}, config=_cb)
            self._last_machines = machines
            for m in machines:
                items.append(
                    {
                        "name": m["Name"],
                        "type": "octopus_machine",
                        "data": {
                            "machine_id": m["Id"],
                            "status": m.get("Status", ""),
                            "is_disabled": m.get("IsDisabled", False),
                        },
                    }
                )
        except Exception as exc:
            log.warning("Octopus machines: %s", exc)
            errors.append(f"machines failed: {exc}")

        # Projects
        try:
            projects = octopus_projects_tool.invoke({}, config=_cb)
            self._last_projects = projects
            for project in projects:
                items.append(
                    {
                        "name": project["Name"],
                        "type": "octopus_project",
                        "data": {
                            "project_id": project["Id"],
                            "slug": project.get("Slug", ""),
                            "lifecycle_id": project.get("LifecycleId", ""),
                        },
                    }
                )
        except Exception as exc:
            log.warning("Octopus projects: %s", exc)
            errors.append(f"projects failed: {exc}")

        return CollectOutcome(items=items, errors=errors)

    # --- run: write the rich detail tables after base upserts resources ----

    def _detail_writers(self, scope, result):
        # MR2 rich tables first (projects/envs/machines/channels/releases/
        # deployments), then the MR9 deep tables (deploy steps, variables[meta],
        # tasks/events, feeds, accounts, teams, users, action templates, machine
        # roles, interruptions). Both run through _write_details (driven by
        # ETLConnector.run()) so any failure is SURFACED on the CollectionRun +
        # result (status="failed") rather than only logged.
        return [
            lambda: self._write_octopus_details(scope),
            lambda: self._write_octopus_deep(scope),
        ]

    def _write_octopus_details(self, scope: str) -> int:
        _cb = {"callbacks": self.callbacks}

        # Reuse anything collect() already fetched; otherwise fetch fresh.
        envs = getattr(self, "_last_envs", None)
        machines = getattr(self, "_last_machines", None)
        projects = getattr(self, "_last_projects", None)
        server = getattr(self, "_last_server", None)

        # id -> name resolution maps (built once)
        env_names: dict[str, str] = {}
        group_names: dict[str, str] = {}
        lifecycle_names: dict[str, str] = {}

        with get_session() as session:
            # --- Project groups (reference table, no Resource) -------------
            groups = []
            try:
                groups = octopus_projectgroups_tool.invoke({}, config=_cb)
                group_names = {g["Id"]: g["Name"] for g in groups}
                for g in groups:
                    self._upsert(
                        session,
                        OctopusProjectGroup,
                        g["Id"],
                        name=g.get("Name", ""),
                        description=g.get("Description"),
                        retention_policy_id=g.get("RetentionPolicyId"),
                    )
            except Exception as exc:
                log.warning("Octopus project groups: %s", exc)

            # --- Lifecycles (reference table, no Resource) -----------------
            try:
                lifecycles = octopus_lifecycles_tool.invoke({}, config=_cb)
                lifecycle_names = {lc["Id"]: lc["Name"] for lc in lifecycles}
                for lc in lifecycles:
                    self._upsert(
                        session,
                        OctopusLifecycle,
                        lc["Id"],
                        name=lc.get("Name", ""),
                        description=lc.get("Description"),
                        phases=lc.get("Phases", []) or [],
                    )
            except Exception as exc:
                log.warning("Octopus lifecycles: %s", exc)

            # --- Environments (linked to Resource) -------------------------
            # DL-C-5: each environment gets its own SAVEPOINT so one bad row
            # (e.g. a flush-time constraint violation) rolls back only that row,
            # never poisoning the shared session and silently dropping every
            # remaining environment (previously this whole block shared one
            # try/except with no SAVEPOINT — a single bad row aborted the run).
            # DL-C-5/AA-R-14: OctopusEnvironment.resource_id is a NOT NULL FK —
            # _resource_id() can return None (Resource row not yet upserted);
            # guard and skip rather than crash the run on an IntegrityError.
            try:
                if envs is None:
                    envs = octopus_environments_tool.invoke({}, config=_cb)
                env_names = {e["Id"]: e["Name"] for e in envs}
            except Exception as exc:
                log.warning("Octopus environments detail: %s", exc)
                envs = envs or []
            envs_written = envs_skipped = 0
            for e in envs or []:
                rid = self._resource_id(session, "octopus_environment", e.get("Name", ""))
                if rid is None:
                    log.warning(
                        "Octopus environment %s (%r) has no matching Resource row — skipping",
                        e.get("Id"),
                        e.get("Name"),
                    )
                    envs_skipped += 1
                    continue
                try:
                    with session.begin_nested():
                        self._upsert(
                            session,
                            OctopusEnvironment,
                            e["Id"],
                            resource_id=rid,
                            name=e.get("Name", ""),
                            description=e.get("Description"),
                            sort_order=e.get("SortOrder", 0) or 0,
                        )
                    envs_written += 1
                except Exception as exc:
                    envs_skipped += 1
                    log.warning("Octopus environment %s skipped: %s", e.get("Id"), exc)
            if envs_skipped:
                log.warning(
                    "Octopus environments: %d written, %d skipped", envs_written, envs_skipped
                )

            # --- Machines (linked to Resource) -----------------------------
            # DL-C-5/AA-R-14: same per-row SAVEPOINT + None-resource_id guard as
            # environments above — OctopusMachine.resource_id is also NOT NULL.
            try:
                if machines is None:
                    machines = octopus_machines_tool.invoke({}, config=_cb)
            except Exception as exc:
                log.warning("Octopus machines detail: %s", exc)
                machines = machines or []
            machines_written = machines_skipped = 0
            for m in machines or []:
                rid = self._resource_id(session, "octopus_machine", m.get("Name", ""))
                if rid is None:
                    log.warning(
                        "Octopus machine %s (%r) has no matching Resource row — skipping",
                        m.get("Id"),
                        m.get("Name"),
                    )
                    machines_skipped += 1
                    continue
                try:
                    with session.begin_nested():
                        endpoint = m.get("Endpoint") or {}
                        tentacle = endpoint.get("TentacleVersionDetails") or {}
                        env_ids = m.get("EnvironmentIds", []) or []
                        self._upsert(
                            session,
                            OctopusMachine,
                            m["Id"],
                            resource_id=rid,
                            name=m.get("Name", ""),
                            status=m.get("Status", "") or "",
                            status_summary=m.get("StatusSummary"),
                            is_disabled=m.get("IsDisabled", False),
                            communication_style=endpoint.get("CommunicationStyle"),
                            tentacle_version=tentacle.get("Version"),
                            uri=endpoint.get("Uri"),
                            roles=m.get("Roles", []) or [],
                            environment_ids=env_ids,
                            environment_names=[env_names.get(eid, eid) for eid in env_ids],
                        )
                    machines_written += 1
                except Exception as exc:
                    machines_skipped += 1
                    log.warning("Octopus machine %s skipped: %s", m.get("Id"), exc)
            if machines_skipped:
                log.warning(
                    "Octopus machines: %d written, %d skipped", machines_written, machines_skipped
                )

            # --- Projects (linked to Resource) -----------------------------
            # Build {project_octopus_id: resource_id} while upserting projects so the
            # deployment write below can link each deployment to its project's
            # Resource (octopus_deployments.resource_id, MR9).
            # DL-C-5/AA-R-14: same per-row SAVEPOINT + None-resource_id guard —
            # OctopusProject.resource_id is also NOT NULL.
            project_resource_ids: dict[str, uuid.UUID] = {}
            try:
                if projects is None:
                    projects = octopus_projects_tool.invoke({}, config=_cb)
            except Exception as exc:
                log.warning("Octopus projects detail: %s", exc)
                projects = projects or []
            projects_written = projects_skipped = 0
            for p in projects or []:
                rid = self._resource_id(session, "octopus_project", p.get("Name", ""))
                if rid is None:
                    log.warning(
                        "Octopus project %s (%r) has no matching Resource row — skipping",
                        p.get("Id"),
                        p.get("Name"),
                    )
                    projects_skipped += 1
                    continue
                project_resource_ids[p["Id"]] = rid
                try:
                    with session.begin_nested():
                        self._upsert(
                            session,
                            OctopusProject,
                            p["Id"],
                            resource_id=rid,
                            name=p.get("Name", ""),
                            slug=p.get("Slug", "") or "",
                            description=p.get("Description"),
                            is_disabled=p.get("IsDisabled", False),
                            project_group_id=p.get("ProjectGroupId"),
                            project_group_name=group_names.get(p.get("ProjectGroupId", "")),
                            lifecycle_id=p.get("LifecycleId"),
                            lifecycle_name=lifecycle_names.get(p.get("LifecycleId", "")),
                            deployment_process_id=p.get("DeploymentProcessId"),
                            included_library_variable_set_ids=(
                                p.get("IncludedLibraryVariableSetIds") or []
                            ),
                        )
                    projects_written += 1
                except Exception as exc:
                    # NOTE: project_resource_ids[p["Id"]] is kept even on a detail-write
                    # failure — rid points at the already-committed generic Resource row
                    # (from collect()), which downstream deployment linking still needs
                    # regardless of whether the octopus_projects detail row itself wrote.
                    projects_skipped += 1
                    log.warning("Octopus project %s skipped: %s", p.get("Id"), exc)
            if projects_skipped:
                log.warning(
                    "Octopus projects: %d written, %d skipped", projects_written, projects_skipped
                )

            # --- Channels + releases (per project) -------------------------
            # Strategy: channels for every project (cheap, default channel each),
            # releases capped at take=5 per project. ~240 + 240 = ~480 sequential
            # GETs — acceptable; correctness of channels/releases preferred.
            project_names = {p["Id"]: p["Name"] for p in (projects or [])}
            channels_written = releases_written = 0
            for p in projects or []:
                pid = p["Id"]
                pname = p.get("Name", "")
                channel_names: dict[str, str] = {}
                # Per-project SAVEPOINT: one project's bad/relative URL or a failed
                # flush rolls back only that project, never poisoning the shared
                # session and silently dropping every remaining project's data.
                try:
                    with session.begin_nested():
                        for ch in octopus_channels_tool.invoke({"project_id": pid}, config=_cb):
                            channel_names[ch["Id"]] = ch.get("Name", "")
                            self._upsert(
                                session,
                                OctopusChannel,
                                ch["Id"],
                                project_id=pid,
                                project_name=pname,
                                name=ch.get("Name", ""),
                                is_default=ch.get("IsDefault", False),
                                lifecycle_id=ch.get("LifecycleId"),
                            )
                            channels_written += 1
                except Exception as exc:
                    log.warning("Octopus channels (project %s): %s", pid, exc)

                try:
                    with session.begin_nested():
                        rel = octopus_releases_tool.invoke(
                            {"project_id": pid, "take": 5}, config=_cb
                        )
                        rel_items = rel.get("Items", []) if isinstance(rel, dict) else rel
                        for r in [_dlp_record(rec, f"release/{pname}") for rec in rel_items]:
                            rrid = self._resource_id(session, "octopus_project", pname)
                            self._upsert(
                                session,
                                OctopusRelease,
                                r["Id"],
                                resource_id=rrid,
                                project_id=r.get("ProjectId", pid),
                                project_name=pname,
                                version=r.get("Version", "") or "",
                                channel_id=r.get("ChannelId"),
                                channel_name=channel_names.get(r.get("ChannelId", "")),
                                assembled=_parse_dt(r.get("Assembled")),
                                release_notes=r.get("ReleaseNotes"),
                            )
                            releases_written += 1
                except Exception as exc:
                    log.warning("Octopus releases (project %s): %s", pid, exc)

            # --- Deployments (bulk via dashboard, no per-deployment task) ---
            deployments_written = 0
            try:
                dash = octopus_dashboard_tool.invoke({}, config=_cb)
                for d in dash.get("Items", []) or []:
                    state = d.get("State")
                    self._upsert(
                        session,
                        OctopusDeployment,
                        d["Id"],
                        # Link deployment → its project's Resource row (MR9).
                        resource_id=project_resource_ids.get(d.get("ProjectId", "")),
                        project_id=d.get("ProjectId", ""),
                        project_name=project_names.get(d.get("ProjectId", "")),
                        release_id=d.get("ReleaseId"),
                        release_version=d.get("ReleaseVersion"),
                        environment_id=d.get("EnvironmentId"),
                        environment_name=env_names.get(d.get("EnvironmentId", "")),
                        task_id=d.get("TaskId"),
                        state=state,
                        created=_parse_dt(d.get("Created")),
                        # Dashboard items carry no StartTime → started_at stays null.
                        started_at=None,
                        completed_at=_parse_dt(d.get("CompletedTime")),
                        duration=d.get("Duration"),
                        # Dashboard has no FinishedSuccessfully; derive from State.
                        finished_successfully=(state == "Success") if state else None,
                    )
                    deployments_written += 1
            except Exception as exc:
                log.warning("Octopus deployments (dashboard): %s", exc)

            # --- Server version (stored on the octopus_server Resource only) -
            if server is not None:
                try:
                    res = (
                        session.query(Resource)
                        .filter_by(domain="octopus", type="octopus_server", name="octopus-server")
                        .first()
                    )
                    if res is not None:
                        res.metadata_ = {"version": server.get("Version", "")}
                except Exception as exc:
                    log.warning("Octopus server detail: %s", exc)

            # No knowledge-graph edges are written here. ``_emit_graph_edges``
            # ran at this point — after all Resource + detail rows were flushed
            # so its resource_id lookups resolved — until P5 deleted it with the
            # ``resource_relationships`` store it wrote into. See its epitaph
            # below.

            session.commit()

        # TRK-245: return the total detail rows actually written so
        # ETLConnector._write_details() accumulates a real, non-zero count into
        # CollectionResult.detail_rows_written / CollectionRun.detail_rows_written
        # (previously this method fell off the end with an implicit ``None``
        # return, so the counter never moved even though the writes above all
        # succeeded).
        return (
            envs_written
            + machines_written
            + projects_written
            + channels_written
            + releases_written
            + deployments_written
        )

    # ── _emit_graph_edges — DELETED (P5). ────────────────────────────────────
    #
    # WHAT IT WROTE: five edge families, all into ``resource_relationships``
    # through a single ``emit_edges_batch`` call at the end, and nothing else.
    # It READ ``octopus_releases``, ``octopus_deployments``, ``octopus_machines``
    # and ``resources``; it wrote no detail row and no ``resources`` row. The
    # detail writers above (environments, machines, projects, channels,
    # releases, deployments, and the MR9 deep-capture tables below) are
    # untouched, as is the ``octopus_server`` version stamp.
    #
    #   * ``octopus_release    BELONGS_TO   octopus_project``
    #   * ``octopus_deployment DEPLOYED_TO  octopus_environment``
    #   * ``octopus_project    DEPLOYED_TO  octopus_environment`` (deduped)
    #   * ``octopus_machine    DEPLOYS_TO   octopus_environment``
    #   * ``octopus_project    MANAGES      octopus_machine`` (derived
    #       transitively, project -> shared environment -> machine)
    #
    # WHY IT IS GONE: the store it wrote into is being dropped (P5 of
    # docs/decisions/2026-08-11-graph-first-architecture.md). Per-type verdict
    # for all five as emitted HERE: **retired domain, zero rows ever**. Octopus
    # Deploy is not deployed on this estate, this collector is retired, and the
    # T3 drop audit measured zero live rows from ``source='octopus_collector'``.
    # Every ``octopus_*`` table these loops queried is empty, so ``edges``
    # never left the empty list and the ``if edges:`` guard never fired.
    # Deleting this loses no capability that ever existed.
    #
    # NONE OF THE FIVE IS A §3.1 CONTAINMENT REFUSAL. Release, project,
    # environment and machine each have their own ``resources`` row written by
    # the detail phase above, so every one of these connects two
    # independently-referrable entities and would pass §3.1 on its own merits.
    # They are deleted because their store is going away and this emitter never
    # populated it — not because the relationships are bookkeeping.
    #
    # Two caveats worth keeping for whoever revives this collector:
    #   - ``MANAGES`` was the weakest of the five. It was not read from
    #     Octopus; it was INFERRED from co-membership of an environment
    #     (every project deploying to env E was asserted to manage every
    #     machine in E), at confidence 1.0. A revival should read a real
    #     ownership signal rather than restore that inference.
    #   - The pre-MR-B code had ``DEPLOYED_TO``/``DEPLOYS_TO`` backwards; the
    #     directions listed above are the corrected, canonical ones in
    #     ``db/relationships.py``'s ``RelationshipType``. Do not re-derive from
    #     the older code.
    #
    # WHERE THE FACTS LIVE: in the detail tables this collector still writes.
    # ``octopus_releases.project_id`` (release -> project),
    # ``octopus_deployments.project_id``/``.environment_id``/``.environment_name``
    # (deployment -> project and -> environment, which is also where
    # project -> environment was derived FROM), and
    # ``octopus_machines.environment_ids`` (machine -> environments). All four
    # questions are column reads on rows the collector already owns; the edges
    # were a second copy.
    #
    # If Octopus is ever deployed, these come back as COLLECTOR DECLARATIONS
    # (``spec.emits_edges``) over those tables, materialised into ``graph_edges``
    # by ``graph_engine`` — never as a re-derivation into the dropped store.
    #
    # Cross-source IS_SAME_AS was already NOT emitted here before this deletion
    # — HostReconcileAgent remains the sole writer of IS_SAME_AS (Task 4.6).

    # --- MR9 deep capture ---------------------------------------------------
    #
    # Twelve additional tables, written AFTER the MR2 detail phase and OUTSIDE the
    # 600s collect() guard (this is in run()'s detail-write phase). Every entity is
    # guarded by its own SAVEPOINT (``session.begin_nested()``) so one bad record
    # never aborts the batch. The two zone-sensitive tables — octopus_variables and
    # octopus_accounts — store METADATA ONLY: the Variable ``Value`` and any account
    # secret/key/token are dropped unconditionally and never even enter the row dict.
    #
    # Per-project work (deployment processes + project variable sets, ~240 each) is
    # FETCHED with a bounded ThreadPoolExecutor (read-only GETs); the DB writes stay
    # serial on the single session. Tasks and events are time-boxed to the last
    # ``octopus_history_days`` days; events additionally honor a hard row cap.

    def _write_octopus_deep(self, scope: str) -> int:
        _cb = {"callbacks": self.callbacks}
        settings = get_settings()
        workers = max(1, int(settings.octopus_fetch_workers or 8))
        history_days = int(settings.octopus_history_days or 365)
        events_cap = int(settings.octopus_events_cap or 50000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)

        projects = getattr(self, "_last_projects", None)

        with get_session() as session:
            if projects is None:
                try:
                    projects = octopus_projects_tool.invoke({}, config=_cb)
                except Exception as exc:
                    log.warning("Octopus deep: projects fetch failed: %s", exc)
                    projects = []
            project_names = {p["Id"]: p.get("Name", "") for p in (projects or [])}

            # 5. machine roles (flat string array) ---------------------------
            total = self._deep_machine_roles(session, _cb)
            # 6. feeds -------------------------------------------------------
            total += self._deep_feeds(session, _cb)
            # 4. action templates -------------------------------------------
            total += self._deep_action_templates(session, _cb)
            # 9. accounts (META ONLY — no secret material) -------------------
            total += self._deep_accounts(session, _cb)
            # 10. teams ------------------------------------------------------
            total += self._deep_teams(session, _cb)
            # 11. users ------------------------------------------------------
            total += self._deep_users(session, _cb)
            # 7. interruptions ----------------------------------------------
            total += self._deep_interruptions(session, _cb)
            # 3. library variable sets (+ their variables, owner=library) ----
            total += self._deep_library_variable_sets(session, _cb, workers)
            # 1 + 2. per-project deployment steps + project variables --------
            total += self._deep_per_project(session, _cb, projects or [], project_names, workers)
            # 8. tasks (last-year cutoff) -----------------------------------
            total += self._deep_tasks(session, _cb, cutoff)
            # 12. events (from= filter + cutoff + hard cap) ------------------
            # DEFAULT OFF (octopus_collect_events): the target's audit events
            # embed Visa-IIN PANs (pipeline test-card logs) that DLP fail-closes
            # on — correct PCI behavior, but it fails the whole run. Skip unless
            # the event stream is known PAN-free. See docs/READONLY-MODEL.md.
            if settings.octopus_collect_events:
                total += self._deep_events(session, _cb, cutoff, events_cap)
            else:
                log.info(
                    "Octopus deep: events phase skipped (octopus_collect_events=False) — "
                    "audit events may contain cardholder data; DLP would fail-close"
                )

            session.commit()

        # TRK-245: return the summed row counts from every deep-writer phase so
        # ETLConnector._write_details() accumulates a real count (see the
        # matching note on _write_octopus_details above).
        return total

    # --- per-entity deep writers (each its own SAVEPOINT(s)) ----------------

    def _deep_machine_roles(self, session, _cb) -> int:
        try:
            with session.begin_nested():
                roles = octopus_machine_roles_tool.invoke({}, config=_cb) or []
                seen: set[str] = set()
                for name in roles:
                    if not isinstance(name, str) or name in seen:
                        continue
                    seen.add(name)
                    self._upsert_detail(
                        session, OctopusMachineRole, {"role_name": name}, ["role_name"]
                    )
            log.info("Octopus deep: %d machine roles", len(seen))
            return len(seen)
        except Exception as exc:
            log.warning("Octopus deep: machine roles failed: %s", exc)
            return 0

    def _deep_feeds(self, session, _cb) -> int:
        try:
            feeds = octopus_feeds_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: feeds fetch failed: %s", exc)
            return 0
        n = 0
        for f in feeds:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusFeed,
                        {
                            "octopus_id": f["Id"],
                            "name": f.get("Name", "") or "",
                            "feed_type": f.get("FeedType"),
                            "feed_uri": f.get("FeedUri"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: feed %s skipped: %s", f.get("Id"), exc)
        log.info("Octopus deep: %d feeds", n)
        return n

    def _deep_action_templates(self, session, _cb) -> int:
        try:
            templates = octopus_action_templates_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: action templates fetch failed: %s", exc)
            return 0
        n = 0
        for t in templates:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusActionTemplate,
                        {
                            "octopus_id": t["Id"],
                            "name": t.get("Name", "") or "",
                            "action_type": t.get("ActionType"),
                            "version": str(t["Version"]) if t.get("Version") is not None else None,
                            "details": self._action_template_details(t),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: action template %s skipped: %s", t.get("Id"), exc)
        log.info("Octopus deep: %d action templates", n)
        return n

    def _deep_accounts(self, session, _cb) -> int:
        # SECURITY: capture name/type/username/scope ONLY. No secret/key/token field
        # is ever read into the row dict.
        try:
            accounts = octopus_accounts_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: accounts fetch failed: %s", exc)
            return 0
        n = 0
        for a in accounts:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusAccount,
                        {
                            "octopus_id": a["Id"],
                            "name": a.get("Name", "") or "",
                            "account_type": a.get("AccountType"),
                            "username": a.get("Username"),
                            "environment_ids": a.get("EnvironmentIds", []) or [],
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: account %s skipped: %s", a.get("Id"), exc)
        log.info("Octopus deep: %d accounts (metadata only, no secrets)", n)
        return n

    def _deep_teams(self, session, _cb) -> int:
        try:
            teams = octopus_teams_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: teams fetch failed: %s", exc)
            return 0
        n = 0
        for t in teams:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusTeam,
                        {
                            "octopus_id": t["Id"],
                            "name": t.get("Name", "") or "",
                            "member_user_ids": t.get("MemberUserIds"),
                            "project_ids": t.get("ProjectIds"),
                            "environment_ids": t.get("EnvironmentIds"),
                            "user_role_ids": t.get("UserRoleIds"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: team %s skipped: %s", t.get("Id"), exc)
        log.info("Octopus deep: %d teams", n)
        return n

    def _deep_users(self, session, _cb) -> int:
        try:
            users = octopus_users_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: users fetch failed: %s", exc)
            return 0
        n = 0
        for u in users:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusUser,
                        {
                            "octopus_id": u["Id"],
                            "username": u.get("Username", "") or "",
                            "display_name": u.get("DisplayName"),
                            "email": u.get("EmailAddress"),
                            "is_active": u.get("IsActive"),
                            "is_service": u.get("IsService"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: user %s skipped: %s", u.get("Id"), exc)
        log.info("Octopus deep: %d users", n)
        return n

    def _deep_interruptions(self, session, _cb) -> int:
        try:
            interruptions = octopus_interruptions_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: interruptions fetch failed: %s", exc)
            return 0
        n = 0
        for i in (_dlp_record(rec, "interruption") for rec in interruptions):
            try:
                form = i.get("Form") or {}
                title = form.get("Title") or i.get("Title")
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusInterruption,
                        {
                            "octopus_id": i["Id"],
                            "title": title,
                            "is_pending": bool(i.get("IsPending", False)),
                            "task_id": i.get("TaskId"),
                            "responsible_team_ids": i.get("ResponsibleTeamIds"),
                            "related_document_ids": i.get("RelatedDocumentIds"),
                            "created": _parse_dt(i.get("Created")),
                            "details": {"can_take_responsibility": i.get("CanTakeResponsibility")},
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: interruption %s skipped: %s", i.get("Id"), exc)
        log.info("Octopus deep: %d interruptions", n)
        return n

    def _deep_library_variable_sets(self, session, _cb, workers) -> int:
        try:
            sets = octopus_library_variable_sets_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: library variable sets fetch failed: %s", exc)
            return 0
        n = 0
        for s in sets:
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusLibraryVariableSet,
                        {
                            "octopus_id": s["Id"],
                            "name": s.get("Name", "") or "",
                            "description": s.get("Description"),
                            "variable_set_id": s.get("VariableSetId"),
                            "content_type": s.get("ContentType"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: library set %s skipped: %s", s.get("Id"), exc)
        log.info("Octopus deep: %d library variable sets", n)

        # Pull each set's variables (owner_type="library"). Bounded-concurrency
        # fetch keyed by VariableSetId; serial write.
        owners = [
            (s["Id"], s.get("Name", ""), s.get("VariableSetId"))
            for s in sets
            if s.get("VariableSetId")
        ]
        var_count = self._fetch_and_write_variables(session, _cb, owners, "library", workers)
        return n + var_count

    def _deep_per_project(self, session, _cb, projects, project_names, workers) -> int:
        # 1. deployment steps — per project deployment process.
        proc_targets = [
            (p["Id"], p.get("Name", ""), p.get("DeploymentProcessId"))
            for p in projects
            if p.get("DeploymentProcessId")
        ]

        def _fetch_process(target):
            pid, pname, proc_id = target
            try:
                return (
                    pid,
                    pname,
                    proc_id,
                    octopus_deployment_process_tool.invoke({"process_id": proc_id}, config=_cb),
                )
            except Exception as exc:
                log.warning(
                    "Octopus deep: deployment process %s (project %s) failed: %s", proc_id, pid, exc
                )
                return pid, pname, proc_id, None

        step_count = 0
        proj_with_steps = 0
        if proc_targets:
            # TRK-083: record queue depth / worker bound before fan-out (additive).
            observe_pool("octopus", "deep_processes", len(proc_targets), workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for pid, pname, proc_id, process in pool.map(_fetch_process, proc_targets):
                    if not process:
                        continue
                    try:
                        with session.begin_nested():
                            # Delete-reinsert per project for clean step ordering.
                            session.query(OctopusDeploymentStep).filter_by(
                                project_octopus_id=pid
                            ).delete()
                            for s_order, step in enumerate(process.get("Steps", []) or []):
                                step_props = step.get("Properties", {}) or {}
                                for action in step.get("Actions", []) or []:
                                    props = action.get("Properties", {}) or {}
                                    # On 3.3.8 ``Octopus.Action.TargetRoles`` lives on the
                                    # STEP Properties (comma-string), not the action's — the
                                    # action's is always None. Read step first, fall back to
                                    # action for any forward-compat layout.
                                    roles = (
                                        step_props.get("Octopus.Action.TargetRoles")
                                        or props.get("Octopus.Action.TargetRoles")
                                        or ""
                                    )
                                    session.add(
                                        OctopusDeploymentStep(
                                            id=uuid.uuid4(),
                                            project_octopus_id=pid,
                                            project_name=pname,
                                            deployment_process_id=proc_id,
                                            step_order=s_order,
                                            step_name=step.get("Name", "") or "",
                                            action_name=action.get("Name"),
                                            action_type=props.get("Octopus.Action.Type")
                                            or action.get("ActionType"),
                                            target_roles=[r for r in roles.split(",") if r],
                                            channels=action.get("Channels", []) or [],
                                            environments=action.get("Environments", []) or [],
                                            feed_id=props.get("Octopus.Action.Package.FeedId"),
                                            package_id=props.get(
                                                "Octopus.Action.Package.PackageId"
                                            ),
                                            condition=step.get("Condition"),
                                            details=self._step_action_details(props),
                                        )
                                    )
                                    step_count += 1
                        proj_with_steps += 1
                    except Exception as exc:
                        log.warning("Octopus deep: steps write (project %s) skipped: %s", pid, exc)
        log.info(
            "Octopus deep: %d deployment-step actions across %d projects",
            step_count,
            proj_with_steps,
        )

        # 2. project variables (owner_type="project") — META ONLY.
        var_owners = [
            (p["Id"], p.get("Name", ""), p.get("VariableSetId"))
            for p in projects
            if p.get("VariableSetId")
        ]
        var_count = self._fetch_and_write_variables(session, _cb, var_owners, "project", workers)
        return step_count + var_count

    def _fetch_and_write_variables(self, session, _cb, owners, owner_type, workers) -> int:
        """Fetch each owner's variable set (concurrent) and write variable METADATA.

        SECURITY: the Variable ``Value`` is NEVER read into the row dict. Only name,
        scope, IsSensitive and IsEditable are captured. Delete-reinsert per owner.
        ``owners`` is a list of (owner_octopus_id, owner_name, variable_set_id).
        Returns the number of variable rows written (0 when there are no owners).
        """
        if not owners:
            return 0

        def _fetch(owner):
            owner_id, owner_name, vset_id = owner
            try:
                return (
                    owner_id,
                    owner_name,
                    octopus_variableset_tool.invoke({"variable_set_id": vset_id}, config=_cb),
                )
            except Exception as exc:
                log.warning("Octopus deep: variable set %s (%s) failed: %s", vset_id, owner_id, exc)
                return owner_id, owner_name, None

        var_count = 0
        owners_done = 0
        # TRK-083: record queue depth / worker bound before fan-out (additive).
        # owner_type ("library"/"project") keeps the two fan-outs on distinct series.
        observe_pool("octopus", f"variables_{owner_type}", len(owners), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for owner_id, owner_name, vset in pool.map(_fetch, owners):
                if not vset:
                    continue
                try:
                    with session.begin_nested():
                        session.query(OctopusVariable).filter_by(
                            owner_type=owner_type, owner_octopus_id=owner_id
                        ).delete()
                        # ordinal disambiguates the same name repeated across scopes.
                        name_ordinals: dict[str, int] = {}
                        for v in vset.get("Variables", []) or []:
                            name = v.get("Name", "") or ""
                            ordinal = name_ordinals.get(name, 0)
                            name_ordinals[name] = ordinal + 1
                            session.add(
                                OctopusVariable(
                                    id=uuid.uuid4(),
                                    owner_type=owner_type,
                                    owner_octopus_id=owner_id,
                                    owner_name=owner_name,
                                    name=name,
                                    ordinal=ordinal,
                                    scope=v.get("Scope") or {},
                                    is_sensitive=bool(v.get("IsSensitive", False)),
                                    is_editable=bool(v.get("IsEditable", True)),
                                    # NO Value — dropped unconditionally (security rule).
                                )
                            )
                            var_count += 1
                    owners_done += 1
                except Exception as exc:
                    log.warning(
                        "Octopus deep: variables write (%s %s) skipped: %s",
                        owner_type,
                        owner_id,
                        exc,
                    )
        log.info(
            "Octopus deep: %d %s variables (metadata only) across %d owners",
            var_count,
            owner_type,
            owners_done,
        )
        return var_count

    def _deep_tasks(self, session, _cb, cutoff) -> int:
        try:
            tasks = octopus_tasks_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            log.warning("Octopus deep: tasks fetch failed: %s", exc)
            return 0
        # Tasks come newest-first; stop at the first one older than the cutoff.
        n = 0
        stopped_early = False
        for t in (_dlp_record(rec, "task") for rec in tasks):
            completed = _parse_dt(t.get("CompletedTime"))
            queued = _parse_dt(t.get("QueueTime"))
            marker = completed or queued
            if marker is not None and marker < cutoff:
                stopped_early = True
                break
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusTask,
                        {
                            "octopus_id": t["Id"],
                            "name": t.get("Name"),
                            "description": t.get("Description"),
                            "task_type": t.get("Name"),
                            "state": t.get("State"),
                            "start_time": _parse_dt(t.get("StartTime")),
                            "completed_time": completed,
                            "duration": t.get("Duration"),
                            "finished_successfully": t.get("FinishedSuccessfully"),
                            "has_warnings_or_errors": t.get("HasWarningsOrErrors"),
                            "error_message": t.get("ErrorMessage"),
                            "server_node": t.get("ServerNodeId"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: task %s skipped: %s", t.get("Id"), exc)
        log.info(
            "Octopus deep: %d tasks captured (cutoff=%s, stopped_early=%s)",
            n,
            cutoff.isoformat(),
            stopped_early,
        )
        return n

    def _deep_events(self, session, _cb, cutoff, events_cap) -> int:
        # Octopus 3.3.8 mis-parses the ``+00:00`` tz suffix of an aware ISO-8601
        # value (the ``+`` decodes as a space), which SILENTLY voids the ``from=``
        # filter — the server then returns the entire ~92k-event audit history and
        # the paginated fetch times out, swallowed below as 0 rows. Send a
        # TIMEZONE-NAIVE, second-precision value so ``from=`` actually bounds the
        # result server-side and pagination terminates at the 1-year cutoff.
        from_iso = cutoff.replace(tzinfo=None).isoformat(timespec="seconds")
        try:
            events = octopus_events_tool.invoke({"from_iso": from_iso}, config=_cb) or []
        except Exception as exc:
            # SURFACE the failure: re-raise so BaseAgent._write_details flips the
            # CollectionRun/result to status="failed" (a future regression shows as
            # an error, not a silent 0). Per-event errors below still skip via the
            # SAVEPOINT — only a fetch-level failure aborts and is reported.
            log.warning("Octopus deep: events fetch failed: %s", exc)
            raise
        EVENT_CAP = events_cap
        n = 0
        truncated = False
        cutoff_stop = False
        dropped_count = 0
        event_iter = (_dlp_record(rec, "event") for rec in events)
        for e in event_iter:
            if n >= EVENT_CAP:
                truncated = True
                # Don't just subtract len(events) - n: `events` is the raw,
                # unfiltered fetch and may contain far more rows than actually
                # fall inside the cutoff window (the 92k-event Octopus bug
                # scenario documented at the top of this file). Only count
                # remaining rows that themselves pass the cutoff-window
                # filter as "dropped by the cap" — everything past the first
                # out-of-window row would have been excluded anyway and must
                # not be counted here.
                occurred = _parse_dt(e.get("Occurred"))
                if occurred is not None and occurred < cutoff:
                    break
                dropped_count += 1
                continue
            occurred = _parse_dt(e.get("Occurred"))
            # Defensive: if from= was ignored and the feed is newest-first, stop at
            # the cutoff so we never ingest the whole audit history.
            if occurred is not None and occurred < cutoff:
                cutoff_stop = True
                break
            try:
                with session.begin_nested():
                    self._upsert_detail(
                        session,
                        OctopusEvent,
                        {
                            "octopus_id": e["Id"],
                            "category": e.get("Category"),
                            "username": e.get("Username"),
                            "occurred": occurred,
                            "message": e.get("Message"),
                            "related_document_ids": e.get("RelatedDocumentIds"),
                        },
                        ["octopus_id"],
                    )
                n += 1
            except Exception as exc:
                log.warning("Octopus deep: event %s skipped: %s", e.get("Id"), exc)
        if truncated:
            log.warning(
                "Events capped at %d rows — oldest events may be missing; "
                "increase octopus_events_cap setting to raise the limit",
                EVENT_CAP,
            )
            try:
                # Deliberate fresh session (not the caller's `session`): this
                # audit row must survive even if the collection transaction
                # rolls back, so it is written and committed independently
                # via a best-effort get_session() rather than joining the
                # in-flight collection session.
                with get_session() as audit_session:
                    audit_session.add(
                        AgentActionLog(
                            agent=type(self).__name__,
                            domain=self.domain,
                            run_id=str(self._active_run_id),
                            tool="truncation",
                            args_summary=json.dumps(
                                {
                                    "cap": EVENT_CAP,
                                    "dropped_count": dropped_count,
                                    "entity": "octopus_events",
                                }
                            ),
                            verdict="allow",
                            status="ok",
                        )
                    )
                    audit_session.commit()
            except Exception:
                log.exception(
                    "Octopus deep: truncation-counter audit row write failed (entity=octopus_events)"
                )
        log.info(
            "Octopus deep: %d events captured (from=%s, cap=%d, truncated=%s, cutoff_stop=%s)",
            n,
            from_iso,
            EVENT_CAP,
            truncated,
            cutoff_stop,
        )
        return n

    @staticmethod
    def _action_template_details(t: dict) -> dict:
        """Capture rarely-queried action-template extras into the details JSONB."""
        return {
            "description": t.get("Description"),
            "community_template_id": t.get("CommunityActionTemplateId"),
            "parameter_count": len(t.get("Parameters", []) or []),
        }

    @staticmethod
    def _step_action_details(props: dict) -> dict:
        """Capture the leftover step/action properties (everything not a typed column).

        Only the typed columns (action type, target roles, feed/package, etc.) are
        promoted; the remaining properties go to ``details`` so the typed columns
        stay focused. No secret material — deployment-process properties are config,
        not credentials (those live in variables/accounts, which are meta-only).
        """
        promoted = {
            "Octopus.Action.Type",
            "Octopus.Action.TargetRoles",
            "Octopus.Action.Package.FeedId",
            "Octopus.Action.Package.PackageId",
        }
        return {k: v for k, v in (props or {}).items() if k not in promoted}

    # --- helpers ------------------------------------------------------------

    # _resource_id migrated to ETLConnector._resource_id (Task 4, phase1/shared-helpers).

    def _upsert(self, session, model, octopus_id: str, **fields):
        """Upsert a detail row keyed on the unique octopus_id. Re-runs update in place."""
        existing = session.query(model).filter_by(octopus_id=octopus_id).first()
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            return existing
        row = model(id=uuid.uuid4(), octopus_id=octopus_id, **fields)
        session.add(row)
        session.flush()
        return row
