"""InventoryReconcileAgent — keep the GitLab Ansible inventory current.

GitLab is the source of truth. When a host discovered in real infra
(Linux/Windows/vSphere/cloud) is missing from the canonical inventory, this
agent opens a GitLab MR that ADDS it under the right group. Add-only: it never
deletes or modifies existing inventory entries (humans merge the MR).

Deterministic by design — host matching and YAML editing must be reliable, so
no LLM is involved.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra_brain.config import Settings

from infra_brain.agents.base import BaseAgent
from infra_brain.agents.inventory_mr import InventoryMRAgent
from infra_brain.db.models import HostPurposeMap, InventoryReconcileEvent, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.gitlab import gitlab_file_tool, gitlab_repository_tree_tool
from infra_brain.tools.host_purpose_map import DEFAULT_FILE_PATH, parse_host_purpose_map
from infra_brain.tools.hostmatch import inventory_host_index, is_host_in_inventory, normalize_host
from infra_brain.tools.iac_reader import parse_ansible_inventory_content_tool
from infra_brain.tools.inventory_render import add_hosts_to_inventory

logger = logging.getLogger(__name__)

# Inventory file name/path patterns (mirrors IaCAgent's discovery set).
_INVENTORY_PATTERNS = ("inventories/", "inventory/", "hosts.yml", "hosts.yaml")

# GitLab #141: mandatory floor for the host-shaped Resource.type filter that
# GitLab #120 introduced (see ``inventory_host_types`` in config.py). Octopus
# Deploy's Resource rows include octopus_server/octopus_environment/
# octopus_project alongside the actual machine/target entities
# (octopus_machine) — only the latter are genuinely discoverable hosts.
# octopus.py's collector never writes a Resource row for a variable, team,
# account, or user (those live in detail-only tables with no Resource FK —
# see db/models/octopus.py), so today the only way a non-host name reaches
# ``get_inventory_gaps()`` is a misconfigured/stale ``inventory_host_types``
# setting (e.g. an env value pinned before #120 landed, which silently
# un-gates octopus back to "no filter"). This floor is merged over whatever
# ``inventory_host_types`` resolves to so octopus can never fall back to
# unfiltered, regardless of settings — belt-and-suspenders against both a
# config regression and any future octopus.py change that starts writing
# Resource rows for non-machine entities.
_MANDATORY_HOST_TYPES: dict[str, set[str]] = {"octopus": {"octopus_machine"}}


def _norm(x: str | None) -> str:
    """Normalize a HostPurposeMap field for equality: treat None and "" alike."""
    return (x or "").strip()


class InventoryReconcileAgent(BaseAgent):
    spec = AgentSpec(
        domain="inventory_reconcile",
        tier=Tier.RECONCILER,
        schedule="0 5 * * *",
        max_staleness=timedelta(hours=26),
        skip_hook=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        """Reconcile discovered hosts against the GitLab inventory; open an MR for
        any additions. Returns [] (work is recorded as InventoryReconcileEvent
        rows + the MR, not as resources)."""
        s = self.settings
        # MR-J item 3: independently gated (host_purpose_map_project_id, NOT
        # inventory_source_project_id below) since it reads a different GitLab
        # source. Runs first so a purpose-map-only deployment (inventory
        # reconcile itself still off) still gets synced.
        #
        # A failure here must still surface via BaseAgent.run() (AA-R-13) —
        # but it must NOT abort the main inventory reconcile below, which is
        # independently gated and healthy even when the purpose-map source
        # is down. Capture the error, let the main path run to completion (or
        # fail on its own, more specific, terms), then re-raise at the end.
        host_purpose_map_error: Exception | None = None
        try:
            self._sync_host_purpose_map()
        except Exception as exc:
            host_purpose_map_error = exc

        result = self._reconcile_main_inventory(s)

        if host_purpose_map_error is not None:
            raise host_purpose_map_error
        return result

    def _reconcile_main_inventory(self, s: "Settings") -> list[dict]:
        """The inventory_source_project_id-gated reconcile path (unchanged
        logic, extracted from collect() so a host_purpose_map sync failure
        can't block it — see collect()'s docstring/comment)."""
        pid = s.inventory_source_project_id
        if not pid:
            logger.info("inventory_source_project_id unset — InventoryReconcileAgent idle")
            return []

        file_path = s.inventory_file_path or self._discover_inventory_path(pid, s.inventory_branch)
        if not file_path:
            logger.warning("No Ansible inventory file found in project %s", pid)
            return []

        try:
            content = gitlab_file_tool.invoke(
                {"project_id": pid, "file_path": file_path, "ref": s.inventory_branch},
                config={"callbacks": self.callbacks},
            )
        except Exception as exc:
            # AA-R-13 (F-007): a GitLab read failure is a REAL error, not "nothing
            # to reconcile". Swallowing it into a bare [] return made BaseAgent.run()
            # record status="completed" with 0 resources for a run that actually
            # failed — indistinguishable from a legitimately quiet reconcile. Log
            # with full context, then re-raise so run() records status="failed".
            logger.exception(
                "Inventory reconcile: failed to read %s from project %s", file_path, pid
            )
            raise RuntimeError(
                f"Inventory reconcile: failed to read {file_path} from project {pid}: {exc}"
            ) from exc

        parsed = parse_ansible_inventory_content_tool.invoke(
            {"content": content}, config={"callbacks": self.callbacks}
        )
        index = inventory_host_index(parsed.get("hosts", {}).keys())

        domains = [d.strip() for d in s.inventory_match_domains.split(",") if d.strip()]
        group_map = self._parse_group_map(s.inventory_group_map)

        missing: list[tuple[str, str, str]] = []  # (host, domain, target_group)
        seen: set[str] = set()
        for host, domain in self._discovered_hosts(domains):
            if is_host_in_inventory(host, index):
                continue
            norm = normalize_host(host)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            group = group_map.get(domain, s.inventory_default_group)
            missing.append((host, domain, group))

        missing = self._filter_already_proposed(missing)
        if not missing:
            logger.info("Inventory reconcile: nothing new to add")
            return []

        # Render the updated inventory (group additions together), add-only.
        new_content = content
        by_group: dict[str, list[str]] = {}
        for host, _domain, group in missing:
            by_group.setdefault(group, []).append(host)
        for group, hosts in by_group.items():
            new_content = add_hosts_to_inventory(new_content, group, hosts)

        reconcile_id = uuid.uuid4()
        description = "Adds discovered hosts missing from the inventory:\n" + "\n".join(
            f"- `{host}` → group `{group}`" for host, _d, group in missing
        )
        # H-6: status="proposed" must mean exactly what it says — an MR was
        # actually opened. Previously an MR-creation failure was logged and
        # swallowed, and MR-disabled fell straight through, and EITHER WAY the
        # events below were written with status="proposed"/mr_url=None. That
        # poisoned _filter_already_proposed() (it excludes anything already
        # status="proposed") into never proposing those hosts again — the
        # agent's entire purpose silently stopped working while every run
        # still reported status="completed". Now: no MR, no "proposed" row.
        if os.getenv("INFRA_BRAIN_MR_ENABLED", "").lower() not in ("1", "true", "yes"):
            logger.info(
                "InventoryReconcileAgent: MR disabled (set INFRA_BRAIN_MR_ENABLED=true to "
                "enable) — %d host(s) detected as missing but NOT recorded as proposed "
                "(nothing was actually proposed): %s",
                len(missing),
                ", ".join(host for host, _d, _g in missing),
            )
            return []

        try:
            mr_url = InventoryMRAgent().propose_inventory_fix(
                drift_event_id=reconcile_id,
                project_id=pid,
                inventory_file_path=file_path,
                corrected_content=new_content,
                description=description,
            )
        except Exception as exc:
            # AA-R-13 (F-007): a real failure, not "nothing to reconcile" — the
            # old code swallowed this and then wrote status="proposed" rows
            # anyway (mr_url=None), permanently excluding these hosts from
            # every future run via _filter_already_proposed(). Re-raise so
            # run() records status="failed" and no bogus row is ever written;
            # the same hosts are re-discovered and retried next run.
            logger.exception("Inventory reconcile: MR creation failed")
            raise RuntimeError(
                f"Inventory reconcile: MR creation failed for project {pid}: {exc}"
            ) from exc

        with get_session() as session:
            for host, domain, group in missing:
                session.add(
                    InventoryReconcileEvent(
                        host=host,
                        domain=domain,
                        target_group=group,
                        status="proposed",
                        mr_url=mr_url,
                    )
                )
            session.commit()

        logger.info(
            "Inventory reconcile: proposed %d host addition(s), MR=%s", len(missing), mr_url
        )
        return []

    # ── helpers ──────────────────────────────────────────────────────────────

    def _sync_host_purpose_map(self) -> int:
        """MR-J item 3: load the curated host purpose/VLAN map and upsert
        HostPurposeMap rows.

        Independently gated by ``host_purpose_map_project_id`` (0 => idle,
        the default) — a separate GitLab source from
        ``inventory_source_project_id`` above. See
        tools/host_purpose_map.py's module docstring for the exact
        (unverified) source-file path/shape this assumes.

        A configured-but-unreachable source is a real failure (mirrors the
        AA-R-13 convention used for the main inventory-file read above) —
        raises so BaseAgent.run() records status="failed" rather than
        silently reporting a healthy run that actually never synced.
        """
        s = self.settings
        pid = s.host_purpose_map_project_id
        if not pid:
            return 0
        file_path = s.host_purpose_map_file_path or DEFAULT_FILE_PATH
        source = f"{pid}:{file_path}"

        try:
            content = gitlab_file_tool.invoke(
                {"project_id": pid, "file_path": file_path, "ref": s.host_purpose_map_branch},
                config={"callbacks": self.callbacks},
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                # The file simply does not exist in the repo. That is the same
                # situation as the empty-map case handled a few lines below
                # (`if not rows: return 0`) — there is nothing to sync — so it
                # must not be a hard failure. It was: the 404 propagated, and
                # because collect() re-raises after computing the main result,
                # the whole inventory reconciliation was computed and then
                # thrown away, 159 escalating runs in a row. Verified against
                # project 113: host_purpose_map.yml is absent from all 327
                # paths in the repo, so the 404 is accurate and permanent until
                # somebody creates the file.
                logger.info(
                    "Host purpose map sync: %s not present in project %s — nothing to sync",
                    file_path,
                    pid,
                )
                return 0
            logger.exception(
                "Host purpose map sync: failed to read %s from project %s", file_path, pid
            )
            raise RuntimeError(
                f"Host purpose map sync: failed to read {file_path} from project {pid}: {exc}"
            ) from exc

        rows = parse_host_purpose_map(content, source=source)
        if not rows:
            logger.info("Host purpose map sync: no host_purpose_map found in %s", source)
            return 0

        with get_session() as session:
            for row in rows:
                existing = session.query(HostPurposeMap).filter_by(hostname=row["hostname"]).first()
                if existing is None:
                    # (1) No DB row yet -> insert with the repo source constant.
                    session.add(HostPurposeMap(**row))
                    continue
                if str(existing.source or "").startswith("ui:"):
                    # (2) Human-authored row is authoritative. Only let the repo
                    # reclaim it once the file has caught up to the human's value
                    # (their persistence MR merged); otherwise leave it untouched.
                    match = (
                        _norm(existing.purpose) == _norm(row["purpose"])
                        and _norm(existing.vlan) == _norm(row["vlan"])
                        and _norm(existing.subnet) == _norm(row["subnet"])
                    )
                    if not match:
                        # Human value differs from file -> human is newer; skip.
                        continue
                    # File matches the human's change -> flip ownership back to repo
                    # (fall through to the normal repo-sync update below).
                # (3) Repo-sourced/other row (or a merged-match flip-back): repo wins.
                existing.purpose = row["purpose"]
                existing.vlan = row["vlan"]
                existing.subnet = row["subnet"]
                existing.source = row["source"]
            session.commit()

        logger.info("Host purpose map sync: upserted %d row(s) from %s", len(rows), source)
        return len(rows)

    def _discover_inventory_path(self, project_id: int, ref: str) -> str | None:
        """Find the first Ansible inventory file in the repo tree."""
        try:
            tree = gitlab_repository_tree_tool.invoke(
                {"project_id": project_id, "path": "", "ref": ref, "recursive": True},
                config={"callbacks": self.callbacks},
            )
        except Exception as exc:
            # AA-R-13 (F-007): a tree-walk failure is a real error, not "no
            # inventory file exists". Returning None here made collect() log a
            # generic "not found" warning and report status="completed" for a
            # run that actually failed to reach GitLab. Re-raise so the caller's
            # failure is distinguishable from a legitimately absent inventory.
            logger.exception("Inventory reconcile: tree walk failed for project %s", project_id)
            raise RuntimeError(
                f"Inventory reconcile: tree walk failed for project {project_id}: {exc}"
            ) from exc
        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            name = item.get("name", "")
            if any(path.startswith(p) or name == p for p in _INVENTORY_PATTERNS) and (
                name.endswith((".yml", ".yaml"))
            ):
                return path
        return None

    def _discovered_hosts(self, domains: list[str]) -> list[tuple[str, str]]:
        """Return (hostname, domain) for resources in the reconcile domains.

        Some domains multiplex several ``Resource.type`` values under one
        domain and only some are actual hosts (e.g. Octopus emits
        octopus_server/octopus_environment/octopus_project alongside the real
        octopus_machine hosts; vSphere emits vsphere_vcenter/vsphere_permission/
        etc. alongside real vsphere_vm/vsphere_host hosts). For domains present
        in ``inventory_host_types``, restrict to the configured host-shaped
        types. Domains not listed there keep the old unfiltered behavior
        (today: linux, windows — both host-only, one Resource.type each).
        """
        if not domains:
            return []
        host_types_map = self._parse_host_types_map(
            getattr(self.settings, "inventory_host_types", "")
        )
        # GitLab #141: apply the mandatory floor regardless of configured
        # settings — a domain listed in _MANDATORY_HOST_TYPES can never be
        # widened back to "unfiltered", even by an empty/stale/misconfigured
        # inventory_host_types value.
        for domain, mandatory_types in _MANDATORY_HOST_TYPES.items():
            host_types_map[domain] = host_types_map.get(domain, set()) | mandatory_types

        filtered_domains = [d for d in domains if d not in host_types_map]
        typed_domains = [d for d in domains if d in host_types_map]

        rows: list[tuple[str, str]] = []
        with get_session() as session:
            if filtered_domains:
                rows.extend(
                    session.query(Resource.name, Resource.domain)
                    .filter(Resource.domain.in_(filtered_domains))
                    .all()
                )
            for domain in typed_domains:
                host_types = host_types_map[domain]
                rows.extend(
                    session.query(Resource.name, Resource.domain)
                    .filter(Resource.domain == domain, Resource.type.in_(host_types))
                    .all()
                )
        return [(name, domain) for name, domain in rows]

    @staticmethod
    def _parse_host_types_map(raw: str) -> dict[str, set[str]]:
        """Parse "domain:type1|type2,domain:type1|type2" into a dict of sets."""
        mapping: dict[str, set[str]] = {}
        for pair in (raw or "").split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            domain, types_raw = pair.split(":", 1)
            domain = domain.strip()
            types = {t.strip() for t in types_raw.split("|") if t.strip()}
            if domain and types:
                mapping[domain] = types
        return mapping

    @staticmethod
    def _parse_group_map(raw: str) -> dict[str, str]:
        """Parse "domain:group,domain:group" into a dict."""
        mapping: dict[str, str] = {}
        for pair in (raw or "").split(","):
            pair = pair.strip()
            if ":" in pair:
                domain, group = pair.split(":", 1)
                if domain.strip() and group.strip():
                    mapping[domain.strip()] = group.strip()
        return mapping

    @staticmethod
    def _filter_already_proposed(
        missing: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Drop hosts that already have an open 'proposed' reconcile event
        (prevents duplicate MRs for the same not-yet-merged host)."""
        if not missing:
            return []
        with get_session() as session:
            existing = {
                row[0]
                for row in session.query(InventoryReconcileEvent.host)
                .filter(InventoryReconcileEvent.status == "proposed")
                .all()
            }
        return [m for m in missing if m[0] not in existing]
