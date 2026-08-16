"""HomelabServicesAgent — lightweight HTTP reachability polling for the
operator's home-lab services (additive scope, requested directly: "develop
integrations with all the knowledge, building new collectors" using the
operator's own Hermes wiki as the knowledge source).

Reads a small JSON manifest (``homelab_services_manifest.json`` by default,
built by hand from ``~/.hermes/wiki/entities/services/*.md`` and
``entities/hosts/*.md`` — see that file's ``$comment`` for provenance/
exclusions) and issues one bare GET per entry with a short timeout. This is
deliberately the same "deterministic yes/no reachability" shape as
``env_probe.py`` (read that module's docstring) rather than an LLM-driven
agent — there is nothing for an LLM to reason about in "did this URL
respond". Unlike env_probe (which proposes *config* gaps for
infra-brain-managed integrations), this collector's job is a recurring
inventory/health snapshot of home-lab services that are NOT modeled
anywhere else in infra-brain, so it runs as an ordinary scheduled
``ETLConnector`` and its results land in ``resources``/``snapshots`` like any
other domain.

Read-only by construction: every network call here goes through
``tools/http_readonly.readonly_get`` (L-8a — previously a bare module-level
``httpx.get``, which is GET-only in effect but bypasses the audit chain the
rest of the read-only model relies on; ``readonly_get`` holds a
``ReadOnlyClient`` that structurally refuses any non-GET/HEAD request, same
as every other HTTP-backed collector/tool) with no request body and no
mutation of the target service. Nothing in this module calls
``gate_external_write`` because nothing in this module performs an external
*write* — that gate exists for sanctioned mutation paths (GitLab MR / Jira /
Confluence), which this collector has none of.

A single unreachable service must never abort the rest of the sweep — each
probe is wrapped in its own try/except (mirrors
``tools/ansible.py``'s per-host/per-probe tolerance pattern and
``ContainerRegistryAgent``'s per-image try/except): an exception downgrades
that one entry to ``status="down"`` and the loop continues.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from infra_brain.db.models import CONFIDENCE_DETERMINISTIC_NAME, GraphEdgeMethod
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, EdgeSpec, NodeSpec, Tier
from infra_brain.tools.http_readonly import readonly_get

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 5.0

# Bundled default manifest, shipped alongside this package (Wave "homelab
# collectors" additive scope). The integration pass should add a
# ``homelab_services_manifest_path`` Settings field (config.py) defaulting to
# this same path, so an operator can point at a different/curated manifest
# without touching code — see this module's own docstring / the task notes
# for the exact field name. Until that field exists, ``collect()`` falls back
# to this constant unless a caller passes ``manifest_path`` explicitly.
DEFAULT_MANIFEST_PATH = str(Path(__file__).resolve().parent.parent / "homelab_services_manifest.json")


def _load_manifest(manifest_path: str) -> list[dict]:
    """Load and lightly validate the services manifest.

    Returns ``[]`` (not an error) for a well-formed-but-empty ``services``
    list — the caller turns that into ``CollectorSkipped``, matching the
    self-skip convention (``ZERO_HOST_INVENTORY_MARKER`` in
    ``tools/ansible.py`` / ``WindowsAgent``'s empty-credential
    ``CollectorSkipped``): an intentionally-empty manifest is a clean no-op,
    not a failure.

    Raises ``CollectorSkipped`` (NOT a generic exception) when the manifest
    file itself is missing or not valid JSON — an unconfigured/misconfigured
    manifest is the same "nothing to do" shape as a genuinely empty one, so
    ``BaseAgent.run()`` records ``status="skipped"`` rather than
    ``status="failed"`` for what is fundamentally a setup gap, not a runtime
    error partway through collection.
    """
    path = Path(manifest_path)
    if not path.exists():
        raise CollectorSkipped(f"homelab services manifest not found at '{manifest_path}'")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorSkipped(f"homelab services manifest at '{manifest_path}' is unreadable: {exc}") from None

    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, list):
        raise CollectorSkipped(
            f"homelab services manifest at '{manifest_path}' has no 'services' list"
        )
    return services


class HomelabServicesAgent(ETLConnector):
    """Polls each manifest-listed home-lab service with a bare GET and records up/down status."""

    spec = AgentSpec(
        domain="homelab_services",
        tier=Tier.COLLECTOR,
        schedule="6,36 * * * *",
        max_staleness=timedelta(hours=2),
        # P1 of docs/decisions/2026-08-11-graph-first-architecture.md — the
        # first collector to declare its graph contribution instead of having
        # it hand-derived in graph_maintenance.py. Everything below is data;
        # no engine code knows this collector exists.
        emits_nodes=(
            NodeSpec(
                type="HomelabService",
                resource_type="homelab_service",
                natural_key="name",
                # `host` is the join key and must be carried onto the node:
                # the engine matches graph-side, so a value that stays behind
                # in resources.metadata is invisible to the edge. `category`
                # and `source` ride along because they are how a reader judges
                # what the service is and how trustworthy `host` is (IaC vs
                # wiki discovery).
                attributes=("host", "category", "source"),
            ),
        ),
        emits_edges=(
            EdgeSpec(
                type="RUNS_ON",
                from_node="HomelabService",
                to_node="LinuxHost",
                from_key="attributes.host",
                to_key="name",
                # The manifest DECLARES the host (ground truth from
                # homelab-ansible's host_vars docker_stacks entries), but what
                # it declares is a NAME, which we then match against a
                # separately-collected linux_host entity. A name match is a
                # deterministic_match, not an FK-strength declared join, so it
                # may not claim 1.000: rename a host in the inventory and this
                # edge mis-resolves. The store enforces that too; declaring it
                # honestly here means the mistake is caught by reading the
                # spec.
                method=GraphEdgeMethod.DETERMINISTIC_MATCH,
                confidence=CONFIDENCE_DETERMINISTIC_NAME,
                # The manifest spells hosts with HYPHENS ("node_a"); the
                # Ansible inventory that produces linux_host resources spells
                # the same machines with UNDERSCORES ("node_a"). Without this
                # fold nothing joins and the relationship stays inexpressible
                # — which is exactly the state commit 8289227 left it in.
                key_normalizer="host",
            ),
        ),
        # How this source names real-world things, for the P2 cross-source
        # resolver. A service is named by the manifest's `name`; it has no
        # hostname/MAC/serial of its own — it is not a machine.
        identity_keys=("name",),
    )

    def collect(self, scope: str = "all", manifest_path: str | None = None) -> CollectOutcome:
        """Probe every service in the manifest.

        ``manifest_path`` defaults to ``DEFAULT_MANIFEST_PATH`` (the bundled
        JSON file) when not passed explicitly — tests and, once wired in,
        the eventual ``settings.homelab_services_manifest_path`` override
        pass this parameter rather than relying on an import-time constant
        being monkeypatched.
        """
        path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
        services = _load_manifest(path)
        if not services:
            raise CollectorSkipped(f"homelab services manifest at '{path}' lists zero services")

        items: list[dict] = []
        errors: list[str] = []

        for entry in services:
            if not isinstance(entry, dict) or not entry.get("name"):
                errors.append(f"skipping malformed manifest entry (missing name): {entry!r}")
                continue
            if not entry.get("url"):
                # url:null is an intentional, documented gap marker (the
                # manifest's own $comment: "url:null entries are honestly-
                # flagged gaps -- no confirmed working URL"), not a parsing
                # failure -- record it as a real item so it's visible in
                # resources/snapshots instead of vanishing into an error log
                # and inflating status="partial" on every single run.
                items.append(self._not_applicable_item(entry))
                continue
            item = self._probe_one(entry)
            items.append(item)

        return CollectOutcome(items=items, errors=errors)

    def _not_applicable_item(self, entry: dict) -> dict:
        """A manifest entry with no confirmed pollable URL -- known service,
        nothing to probe. See collect()'s url:null handling."""
        return {
            "name": entry["name"],
            "type": "homelab_service",
            "data": {
                "category": entry.get("category", ""),
                # The manifest declares which host each service runs on --
                # ground truth from homelab-ansible's inventory/host_vars
                # docker_stacks entries. It was read here and then dropped,
                # so the resource row could never say where a service lives.
                # That is the ONLY field linking a service to a machine, and
                # without it "what runs on media_host" / "what breaks if node_a
                # dies" are unanswerable. Carried through for BOTH item
                # shapes deliberately: a url:null service still runs
                # somewhere, and is arguably the more important case since
                # nothing else can locate it.
                "host": entry.get("host"),
                "source": entry.get("source"),
                "url": None,
                "status": "not_applicable",
                "http_status": None,
                "reason": entry.get("note") or "no confirmed pollable endpoint",
            },
        }

    def _probe_one(self, entry: dict) -> dict:
        """Probe a single manifest entry. Never raises — an unreachable
        service becomes ``status="down"`` with ``http_status=None`` rather
        than aborting the sweep (mirrors ``run_ansible_pending_updates``'s
        per-manager tolerance and ``_merge_enrichment_facts``'s per-probe
        try/except in ``tools/ansible.py``)."""
        name = entry["name"]
        url = entry["url"]
        category = entry.get("category", "")
        http_status: int | None = None
        status = "down"

        try:
            # L-8a: routed through tools/http_readonly.readonly_get (a
            # ReadOnlyClient that structurally refuses any non-GET/HEAD
            # request) rather than a bare module-level httpx.get, matching
            # every other HTTP-backed collector/tool in this codebase (see
            # grafana_tool.py, prometheus_tool.py, wazuh_tool.py, etc.) so
            # this probe rides the same GET-only-client shape the read-only
            # model's structural layer (R2 layer 1) relies on.
            resp = readonly_get(url, timeout=_PROBE_TIMEOUT_SECONDS)
            http_status = resp.status_code
            # Mirrors ContainerRegistryAgent/env_probe's "< 500 counts as up"
            # convention -- many of these services legitimately 404/401 on a
            # bare root GET while genuinely being up (e.g. openui-dashboard's
            # own wiki page notes a plain 404 root is normal for its MCP
            # bridge sibling); only a connection failure or a 5xx server
            # error means truly unreachable.
            status = "up" if http_status < 500 else "down"
        except Exception as exc:  # noqa: BLE001 - a single malformed/unreachable
            # entry must never abort the sweep. `readonly_get()` (like the
            # bare `httpx.get()` it replaces) does not confine itself to
            # `httpx.HTTPError` -- a manifest URL with a stray
            # embedded control character, a trailing newline (a plausible
            # copy/paste artifact from the wiki source this manifest is built
            # from), or one that's simply too long raises `httpx.InvalidURL`,
            # which is NOT an HTTPError subclass (verified against the
            # installed httpx: InvalidURL.__mro__ is (InvalidURL, Exception),
            # no HTTPError in it). Narrowly catching only HTTPError here used
            # to mean one such entry raised straight out of this method, past
            # `collect()`'s unguarded `item = self._probe_one(entry)` call
            # (no try/except there), silently dropping every entry after it
            # in manifest order for that whole collection run -- precisely
            # the "single unreachable service aborts the rest of the sweep"
            # failure this docstring already claims cannot happen. Mirrors
            # ContainerRegistryAgent's per-image `except Exception` (this
            # module's own docstring already cites that agent as the pattern
            # this follows).
            logger.info("HomelabServicesAgent: %s (%s) unreachable: %s", name, url, exc)

        return {
            "name": name,
            "type": "homelab_service",
            "data": {
                # See _not_applicable_item for why host/source are carried.
                "host": entry.get("host"),
                "source": entry.get("source"),
                "category": category,
                "url": url,
                "status": status,
                "http_status": http_status,
            },
        }
