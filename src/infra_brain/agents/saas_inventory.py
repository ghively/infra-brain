"""SaaSInventoryAgent — third-party SaaS application + API-key inventory (GitLab #103).

Tracks shadow-IT / API-key-sprawl visibility: which SaaS applications are in
use and their API keys' METADATA ONLY (name/scope/created_at/last_used_at/
owner — never the key value). Same safety bar as the secrets-manager
candidate (#98).

``collect()`` emits generic ``Resource`` rows (``type="saas_application"``);
the rich detail tables (``saas_applications``, ``saas_api_key_metadata``)
are written in the detail-write phase — the OctopusAgent
collect()/_detail_writers() split. No graph edge is written: the
``USES_SAAS_APP`` deriver that used to run here was deleted in P5 with the
``resource_relationships`` store it wrote into (see ``_emit_saas_edges``'
epitaph below).

All upstream calls are read-only GETs through ``tools/saas_inventory_tool.py``
(itself built on ``tools/http_readonly.readonly_get``). Empty config
(``settings.saas_admin_url == ""``) makes ``collect()`` a clean no-op — this
agent has no live SaaS admin API configured by default.
"""

import logging
from datetime import datetime, timedelta

from infra_brain.db.models import SaaSApiKeyMetadata, SaaSApplication
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.saas_inventory_tool import saas_api_keys_tool, saas_applications_tool

log = logging.getLogger(__name__)

# Explicit metadata-only allowlist for an API-key row. Any field not named
# here (in particular the key VALUE/secret) is never carried into the row
# dict — mirrors OctopusAgent._fetch_and_write_variables (Value dropped
# unconditionally). The tool boundary (saas_inventory_tool.py) already
# strips secret-shaped fields as defense-in-depth; this allowlist is the
# second, independent layer.
_KEY_METADATA_FIELDS = ("scope", "created_at", "last_used_at", "owner", "is_active")


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class SaaSInventoryAgent(ETLConnector):
    """Collects third-party SaaS applications + API-key metadata (read-only)."""

    spec = AgentSpec(
        domain="saas_inventory",
        tier=Tier.COLLECTOR,
        schedule="45 3 * * *",
        max_staleness=timedelta(hours=26),
        # DELIBERATELY NOT retired (2026-08-12) — flagged as a candidate, then
        # left on. Unlike vsphere/Rapid7/Octopus/Okta, this collector names no
        # specific enterprise vendor: it reads a generic SaaS-admin API given by
        # `saas_admin_url`, and tracking which SaaS subscriptions exist and what
        # API keys they have is a real home-lab concern. No evidence was found
        # that its upstream cannot exist here, and the standing instruction is
        # to err toward leaving a collector ON. Revisit only if the maintainer
        # confirms no SaaS-admin API will ever be pointed at it. It self-skips
        # cleanly while `saas_admin_url` is empty.
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        items: list[dict] = []
        errors: list[str] = []
        _cb = {"callbacks": self.callbacks}

        if not self.settings.saas_admin_url:
            # Self-skip, not a "clean no-op" completed run. Returning an empty
            # CollectOutcome here recorded status="completed" with 0 rows, which
            # the R3 completeness monitor correctly reads as "silently empty?"
            # — it had escalated 168x in a row on this fleet for an integration
            # that was simply never configured. "skipped" is the outcome that
            # actually describes an absent dependency, and it is what every
            # sibling collector already raises.
            self._last_apps = []
            self._last_keys = {}
            raise CollectorSkipped(
                "saas_admin_url not configured; set SAAS_ADMIN_URL to enable SaaS inventory"
            )

        try:
            apps = saas_applications_tool.invoke({}, config=_cb)
        except Exception as exc:
            log.warning("SaaSInventoryAgent: saas_applications_tool failed: %s", exc)
            self._last_apps = []
            self._last_keys = {}
            return CollectOutcome(items=[], errors=[f"applications failed: {exc}"])

        self._last_apps = apps or []
        self._last_keys = {}
        for app in self._last_apps:
            name = app.get("name") or app.get("Name") or ""
            if not name:
                continue
            items.append(
                {
                    "name": name,
                    "type": "saas_application",
                    "data": {
                        "vendor": app.get("vendor", ""),
                        "category": app.get("category", ""),
                        "owner_team": app.get("owner_team", ""),
                    },
                }
            )
            try:
                keys = saas_api_keys_tool.invoke({"app": name}, config=_cb)
                self._last_keys[name] = keys
            except Exception as exc:
                log.warning("SaaSInventoryAgent: api keys fetch failed for %s: %s", name, exc)
                errors.append(f"api keys failed for {name}: {exc}")

        return CollectOutcome(items=items, errors=errors)

    def _detail_writers(self, scope, result):
        return (lambda: self._write_saas_details(scope),)

    def _write_saas_details(self, scope: str) -> int:
        """Upsert SaaSApplication + SaaSApiKeyMetadata, then emit USES_SAAS_APP edges.

        Every key row is built from ``_KEY_METADATA_FIELDS`` only — the key
        VALUE is never in the source dict by the time it reaches this
        function (stripped at the tool boundary), and this allowlist is a
        second independent guarantee it can never be persisted even if that
        boundary were ever weakened.
        """
        apps = getattr(self, "_last_apps", None) or []
        keys_by_app = getattr(self, "_last_keys", None) or {}
        count = 0

        with get_session() as session:
            for app in apps:
                name = app.get("name") or app.get("Name") or ""
                if not name:
                    continue
                rid = self._resource_id(session, "saas_application", name)
                if rid is None:
                    continue
                existing = session.query(SaaSApplication).filter_by(resource_id=rid).one_or_none()
                if existing is None:
                    existing = SaaSApplication(resource_id=rid, name=name)
                    session.add(existing)
                existing.vendor = app.get("vendor", "") or None
                existing.category = app.get("category", "") or None
                existing.owner_team = app.get("owner_team", "") or None
                existing.details = {k: v for k, v in app.items() if k not in ("name", "Name")}
                count += 1

                for key in keys_by_app.get(name, []) or []:
                    key_name = key.get("key_name") or key.get("name") or key.get("Id") or ""
                    if not key_name:
                        continue
                    existing_key = (
                        session.query(SaaSApiKeyMetadata)
                        .filter_by(app_name=name, key_name=key_name)
                        .one_or_none()
                    )
                    if existing_key is None:
                        existing_key = SaaSApiKeyMetadata(app_name=name, key_name=key_name)
                        session.add(existing_key)
                    for field in _KEY_METADATA_FIELDS:
                        if field not in key:
                            continue
                        value = key[field]
                        if field in ("created_at", "last_used_at"):
                            value = _parse_dt(value)
                        setattr(existing_key, field, value)
                    count += 1

            session.commit()

        return count

    # ── _emit_saas_edges — DELETED (P5). ────────────────────────────────────
    #
    # WHAT IT WROTE: one ``USES_SAAS_APP`` edge per ``SaaSApplication`` with a
    # non-empty ``owner_team``, from a ``gitlab_project``/``octopus_project``
    # Resource matched by exact name, into ``resource_relationships``. That was
    # its only write — it read ``saas_applications`` + ``resources`` and called
    # ``emit_edges_batch``; the detail tables above are untouched by its removal.
    #
    # WHY IT IS GONE: the store it wrote into is being dropped (P5 of
    # docs/decisions/2026-08-11-graph-first-architecture.md). Per-type verdict
    # for ``USES_SAAS_APP``: **unconfigured domain, zero rows ever**.
    # SaaSInventoryAgent raises ``CollectorSkipped`` unless ``saas_admin_url``
    # is set, and it is not set on this estate — so ``saas_applications`` has
    # never held a row, ``owner_team`` has never been populated, and this
    # method has never appended a single edge to its list. The T3 drop audit
    # measured zero live ``USES_SAAS_APP`` rows. Deleting it removes no
    # capability anyone has ever had.
    #
    # NOT a §3.1 containment refusal: application↔owning-team IS a genuine
    # relationship between two independently-referrable entities. If this
    # collector is ever configured, the edge comes back as a COLLECTOR
    # DECLARATION (``spec.emits_edges``) over ``saas_applications.owner_team``,
    # materialised into ``graph_edges`` by ``graph_engine`` — never as a
    # re-derivation into the dropped store. The fact itself is not lost either
    # way: ``owner_team`` is a column on ``saas_applications``, so "who owns
    # this app?" is a column read, not a graph walk.
