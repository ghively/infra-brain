"""IdentityAgent — identity/SSO/RBAC audit across integrated systems.

GitLab issue #102 ("[roadmap] New agent: identity/SSO/RBAC audit across
integrated systems"): pulls the canonical identity roster from the org's IdP
(Okta — see ``tools/identity.py``) and reconciles "who has access to what
across GitLab/Octopus/vSphere/etc" as a first-class finding, distinct from the
existing per-system access tables (VspherePermission's ``GRANTED_ON``,
OctopusTeam membership).

``collect()`` emits generic ``identity_principal`` / ``identity_group``
Resource rows (so the base pipeline snapshots + drift-diffs a principal's
status/group membership like every other collector). ``run()`` is overridden
(the vsphere/octopus "Stage 2" split) to additionally write the normalized
relational detail tables (``identity_principals``,
``identity_group_memberships``) via ``_write_details``, so a structural
failure marks the CollectionRun ``failed`` instead of silently leaving the
rich tables empty (per the vsphere docstring's own contract this file
follows).

It used to emit ``IS_PRINCIPAL_FOR`` edges as a second step — the "one
identity, N system grants" rollup GitLab #102 asked for. That deriver was
deleted in P5 along with the ``resource_relationships`` store it wrote into;
see the epitaph where ``_emit_is_principal_for_edges`` stood.

Read-only: every Okta call in ``tools/identity.py`` is a GET through
``ReadOnlyClient`` (structurally refuses non-GET/HEAD at the transport
layer) — this collector never mutates the IdP or any downstream system.
Gracefully degrades (raises ``CollectorSkipped``) when ``okta_url`` /
``okta_api_token`` are unconfigured, matching the vsphere/octopus/rapid7
"unconfigured external system" convention.
"""

import logging
from datetime import UTC, datetime, timedelta

from infra_brain.db.models import IdentityGroupMembership, IdentityPrincipal
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.identity import (
    okta_group_members_tool,
    okta_groups_tool,
    okta_users_tool,
)

logger = logging.getLogger(__name__)

_SOURCE = "okta"


def _now() -> datetime:
    return datetime.now(UTC)


# ``_norm`` (lowercase/strip for cross-system username matching) lived here
# and was deleted with ``_emit_is_principal_for_edges``, its only caller — it
# existed solely to hit graph_maintenance's convergence-node cache key.


class IdentityAgent(ETLConnector):
    spec = AgentSpec(
        domain="identity",
        tier=Tier.COLLECTOR,
        schedule="15 3 * * *",
        max_staleness=timedelta(hours=26),
        # 2026-08-12: retired. This collector talks to Okta specifically
        # (okta_url/okta_api_token, see collect() below) — an enterprise IdP
        # with no home-lab counterpart. The 2026-08-02 coverage audit is
        # explicit that IdentityAgent "is credential-gated and genuinely
        # empty", and that the 53 rows carrying domain="identity" are
        # graph_maintenance user_account nodes, NOT this agent's output — so
        # retiring it does not remove any data anyone is reading. Re-enable
        # with COLLECTION_REVIVED_DOMAINS=identity.
        retired=True,
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        if not self.settings.okta_url or not self.settings.okta_api_token:
            raise CollectorSkipped("okta_url/okta_api_token not configured")

        _cb = {"callbacks": self.callbacks}
        items: list[dict] = []
        errors: list[str] = []

        try:
            users = okta_users_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("Okta users fetch failed: %s", exc)
            errors.append(f"users failed: {exc}")
            users = []

        try:
            groups = okta_groups_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("Okta groups fetch failed: %s", exc)
            errors.append(f"groups failed: {exc}")
            groups = []

        # Group membership: one GET per group. Errors on an individual group
        # are logged + skipped (that group's memberships are simply absent
        # this run) rather than aborting the whole pass — matches vsphere's
        # per-entity savepoint-skip philosophy.
        memberships: dict[str, list[dict]] = {}
        for group in groups:
            gid = group.get("id")
            if not gid:
                continue
            try:
                members = okta_group_members_tool.invoke({"group_id": gid}, config=_cb) or []
                memberships[gid] = members
            except Exception as exc:
                logger.warning("Okta group %s members fetch failed: %s", gid, exc)
                errors.append(f"group {gid} members failed: {exc}")

        for user in users:
            profile = user.get("profile") or {}
            uid = user.get("id")
            if not uid:
                continue
            login = profile.get("login") or profile.get("email") or uid
            items.append(
                {
                    "name": login,
                    "type": "identity_principal",
                    "data": {
                        "external_id": uid,
                        "status": user.get("status", ""),
                        "email": profile.get("email"),
                        "login": login,
                    },
                }
            )

        for group in groups:
            gid = group.get("id")
            if not gid:
                continue
            profile = group.get("profile") or {}
            name = profile.get("name") or gid
            items.append(
                {
                    "name": name,
                    "type": "identity_group",
                    "data": {
                        "external_id": gid,
                        "type": group.get("type", ""),
                        "member_count": len(memberships.get(gid, [])),
                    },
                }
            )

        # Cache for the relational detail-write phase so we don't re-fetch.
        self._last_users = users
        self._last_groups = groups
        self._last_memberships = memberships
        return CollectOutcome(items=items, errors=errors)

    # ------------------------------------------------------------------
    # Relational detail-write phase (Stage 2, mirrors vsphere/octopus)
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        return [self._write_identity_details]

    def _write_identity_details(self) -> int:
        users = getattr(self, "_last_users", None) or []
        groups = getattr(self, "_last_groups", None) or []
        memberships = getattr(self, "_last_memberships", None) or {}

        group_names: dict[str, str] = {}
        for group in groups:
            gid = group.get("id")
            if gid:
                group_names[gid] = (group.get("profile") or {}).get("name") or gid

        n = 0
        principal_id_by_external_id: dict[str, object] = {}
        with get_session() as session:
            for user in users:
                uid = user.get("id")
                if not uid:
                    continue
                profile = user.get("profile") or {}
                row = {
                    "source": _SOURCE,
                    "external_id": uid,
                    "username": profile.get("login") or profile.get("email") or uid,
                    "email": profile.get("email"),
                    "display_name": (
                        (profile.get("firstName") or "") + " " + (profile.get("lastName") or "")
                    ).strip()
                    or None,
                    "status": user.get("status", ""),
                    "is_service_account": bool(profile.get("userType") == "service"),
                    "details": profile,
                    "last_seen": _now(),
                }
                try:
                    with session.begin_nested():
                        self._upsert_detail(
                            session, IdentityPrincipal, row, ["source", "external_id"]
                        )
                    n += 1
                except Exception as exc:
                    logger.warning("Okta principal %s skipped: %s", uid, exc)
            session.commit()

            # Resolve principal_id per external_id now that principals are committed.
            for principal in session.query(IdentityPrincipal).filter_by(source=_SOURCE).all():
                principal_id_by_external_id[principal.external_id] = principal.id

            for gid, members in memberships.items():
                for member in members:
                    mid = member.get("id")
                    if not mid:
                        continue
                    principal_id = principal_id_by_external_id.get(mid)
                    if principal_id is None:
                        continue
                    mrow = {
                        "principal_id": principal_id,
                        "source": _SOURCE,
                        "group_external_id": gid,
                        "group_name": group_names.get(gid, gid),
                        "last_seen": _now(),
                    }
                    try:
                        with session.begin_nested():
                            self._upsert_detail(
                                session,
                                IdentityGroupMembership,
                                mrow,
                                ["principal_id", "source", "group_external_id"],
                            )
                        n += 1
                    except Exception as exc:
                        logger.warning(
                            "Okta membership principal=%s group=%s skipped: %s",
                            mid,
                            gid,
                            exc,
                        )
            session.commit()

        return n

    # ── _emit_is_principal_for_edges — DELETED (P5). ────────────────────────
    #
    # WHAT IT WROTE: one ``IS_PRINCIPAL_FOR`` edge per Okta user whose
    # normalized login/email matched an existing ``identity``/``user_account``
    # convergence Resource, from the principal's own ``identity_principal``
    # Resource, into ``resource_relationships``. That was its only write — it
    # read ``resources`` twice and called ``emit_edges_batch``. The relational
    # detail tables (``identity_principals``, ``identity_group_memberships``)
    # are written above and are untouched by its removal.
    #
    # WHY IT IS GONE: the store it wrote into is being dropped (P5 of
    # docs/decisions/2026-08-11-graph-first-architecture.md). Per-type verdict
    # for ``IS_PRINCIPAL_FOR``: **retired domain, zero rows ever**. This
    # collector is ``retired=True`` (see ``spec`` below) and was credential-
    # gated on ``okta_url``/``okta_api_token`` before that — an enterprise IdP
    # with no counterpart on this estate. The 2026-08-02 coverage audit found
    # IdentityAgent "credential-gated and genuinely empty", and the T3 drop
    # audit measured zero live ``IS_PRINCIPAL_FOR`` rows. This method never ran
    # against a real Okta, so it never emitted an edge. Nothing is lost.
    #
    # NOT a §3.1 containment refusal: IdP-principal ↔ per-system-account IS a
    # genuine relationship between two independently-referrable entities — it
    # is exactly the "one identity, N system grants" rollup GitLab #102 asked
    # for. It is deleted because its store is going away and its source has
    # never produced data, not because the relationship is bookkeeping. If
    # Okta (or a successor IdP) is ever wired up, the edge returns as a
    # COLLECTOR DECLARATION (``spec.emits_edges``) over ``identity_principals``,
    # materialised into ``graph_edges`` by ``graph_engine`` — never as a
    # re-derivation into the dropped store. Note the convergence ``user_account``
    # nodes it targeted are graph_maintenance's output, not this agent's; they
    # are unaffected either way.
