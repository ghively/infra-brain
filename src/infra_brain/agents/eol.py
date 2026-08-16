"""EOLAgent — auto-derive the EOL registry from collected OS inventory.

Previous flow was inverted: it read whatever ``eol_registry`` already contained
(populated only by the manual ``add_eol_product`` MCP tool) and re-fetched cycle
data for those names. That meant the registry never self-populated.

This agent INVERTS that: it derives the set of distinct OSes actually present in
the fleet from collected inventory, normalizes each to an endoflife.date
(product, cycle), looks up the EOL date once per product, computes a PCI risk
score from EOL proximity, and UPSERTS ``eol_registry`` keyed on ``asset_name``.

OS-string sources (use whatever is populated; any may be sparse/empty):
  * vsphere_vms.guest_full_name        — cleanest, e.g. "CentOS 7 (64-bit)"
  * Resource(domain="linux").metadata  — distro + version
  * Resource(domain="windows").metadata — os_name + os_version
  * Resource(domain="vuln", r7_asset).metadata — os

Manual ``add_eol_product`` entries coexist: both write keyed on ``asset_name``,
so a manual entry and a derived one for the same friendly label MERGE rather than
duplicate.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.db.models import EolRegistry, HostIdentity, R7Asset, Resource, VsphereVm
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectOutcome, ETLConnector, ReconcileScope
from infra_brain.etl.spec import (
    AgentSpec,
    ChildSpec,
    EdgeDirection,
    EdgeSpec,
    NodeSpec,
    RowGather,
    Tier,
)
from infra_brain.tools.eol import eol_cycles_tool, normalize_fingerprint, normalize_os_string
from infra_brain.tools.hostmatch import normalize_host

# Migration path suggestions keyed by lowercase substring of the asset_name.
# Iteration order matters: more-specific keys must appear before broader ones
# (e.g. "centos linux 7" before "centos").
#
# TRK-193 sub-bug 1: the bare "centos" key used to sit after "centos 7" with
# no "centos 6" entry of its own -- since dict lookup here is
# "does this key appear anywhere in the lowercased asset name" (see
# _suggest_migration below), a CentOS 6 asset_name ("CentOS 6", produced by
# tools/eol.py's `cent\s*os.*\b6\b` rule) matched neither "centos linux 7" nor
# "centos 7", fell through to the bare "centos" key, and got the SAME target
# as CentOS 7/8/9: "Migrate to Rocky Linux 9". That is not a supported/sane
# upgrade path -- Rocky 9 requires a much newer base than CentOS 6 can hop to
# directly. Rather than invent a specific real-world-correct intermediate
# target (which would require guessing at OS support timelines this module
# has no authority over), CentOS 6 now gets an explicit "needs a human
# decision" result instead of a plausible-sounding wrong one. This entry MUST
# stay before the bare "centos" key below so it is matched first.
_CENTOS_6_NO_DIRECT_PATH = (
    "No supported direct migration path from CentOS 6 (EOL) -- Rocky Linux 9 "
    "and other current targets require a newer base than a CentOS 6 host can "
    "hop to directly. Requires a staged intermediate migration (e.g. via "
    "CentOS 7/8 or an in-place OS conversion tool) or a human migration "
    "decision; do not treat this as a one-step upgrade."
)

MIGRATION_MAP: dict[str, str] = {
    "windows server 2012 r2": "Upgrade to Windows Server 2022",
    "windows server 2012": "Upgrade to Windows Server 2022",
    "windows server 2016": "Upgrade to Windows Server 2022",
    "windows server 2019": "Upgrade to Windows Server 2022",
    "centos linux 7": "Migrate to Rocky Linux 9",
    "centos 7": "Migrate to Rocky Linux 9",
    "centos linux 6": _CENTOS_6_NO_DIRECT_PATH,
    "centos 6": _CENTOS_6_NO_DIRECT_PATH,
    "centos": "Migrate to Rocky Linux 9",
    "ubuntu 20.04": "Upgrade to Ubuntu 24.04 LTS",
    "ubuntu 18.04": "Upgrade to Ubuntu 24.04 LTS",
    "rhel 7": "Upgrade to RHEL 9",
    "red hat enterprise linux 7": "Upgrade to RHEL 9",
    "debian 10": "Upgrade to Debian 12",
    "debian 9": "Upgrade to Debian 12",
}


def _suggest_migration(product_name: str) -> str | None:
    """Return the first MIGRATION_MAP match for *product_name*, or None.

    Matching is case-insensitive substring: the map key must appear anywhere
    in the lowercased product name.
    """
    lower = product_name.lower()
    for key, suggestion in MIGRATION_MAP.items():
        if key in lower:
            return suggestion
    return None


logger = logging.getLogger(__name__)


# --- graph contribution (TRK-359) -------------------------------------------
#
# ``EolProduct`` + ``<LinuxHost> ─RUNS_EOL→ <EolProduct>`` — the second junction
# declaration, restoring the second of P5's two accepted losses. eol is the
# natural owner (the deleted deriver's own epitaph named it): it writes every
# ``eol_registry`` row this reads.
#
# THE SHAPE. ``eol_registry`` hangs DIRECTLY off ``resources`` —
# ``resource_id`` is the representative host the product was derived from — so
# the junction row's ANCHOR is itself the entity the edge needs. That is the
# ``RowGather(path=())`` anchor-gather shape: the product node (one per
# distinct ``asset_name``, a shared value node like ContainerImage) carries the
# names of the host resources its registry rows anchor on, and the edge fans
# out of that list with the vocabulary that already existed
# (``from_key_multi`` + ``EdgeDirection.INVERSE`` → stored host → product,
# read "host RUNS_EOL product" per the taxonomy).
#
# ANCHOR SCOPE: ``domain="linux", resource_type="linux_host"`` — another
# collector's rows, which is what ``NodeSpec.domain`` exists for (eol asserts
# ABOUT hosts; its own rows are the registry). Three anchor populations the
# deleted deriver saw are deliberately out of scope, each on the record:
#   * minted ``eol/product`` resources (a registry row with no known host):
#     the deriver linked nothing for these either (its self-loop guard); here
#     they simply produce no node. The product stays fully queryable in
#     ``eol_registry`` and as the ``eol_cycle`` resource this agent collects.
#   * vsphere/windows/r7 representative hosts: retired domains on this estate,
#     zero live rows — same one-line-per-node-type revival note as
#     ANSIBLE_MANAGES' Windows note in iac.py.
#
# ``deterministic_match`` / 0.900, NOT the FK-strength 1.000 the registry row
# itself would justify: the engine matches GRAPH-side, so the anchor's
# ``resources.id`` is degraded to its NAME joined against the LinuxHost node
# through the ``host`` fold — a display-name join, and the honesty rule prices
# it as one. (Both sides come from the same ``resources.name`` column today,
# but a rename between passes can still mis-pair, which 1.000 would deny.)
_EOL_PRODUCT_PATH = (("eol_registry", "resource_id"),)
_EOL_PRODUCT = ChildSpec(key="product", path=_EOL_PRODUCT_PATH, column="asset_name")
_EOL_PRODUCT_HOSTS = RowGather(key="hosts", path=(), column="name")
_EOL_PRODUCT_NODE = NodeSpec(
    type="EolProduct",
    resource_type="linux_host",
    domain="linux",
    natural_key="rows.product",
    name="rows.product",
    resource_backed=False,
    from_rows=_EOL_PRODUCT,
    row_gathers=(_EOL_PRODUCT_HOSTS,),
)
_EOL_RUNS_EOL_EDGES = (
    EdgeSpec(
        type="RUNS_EOL",
        from_node="EolProduct",
        to_node="LinuxHost",
        from_key=f"attributes.{_EOL_PRODUCT_HOSTS.key}",
        to_key="name",
        from_key_multi=True,
        direction=EdgeDirection.INVERSE,
        key_normalizer="host",
        method="deterministic_match",
        confidence=Decimal("0.900"),
    ),
)


class EOLAgent(ETLConnector):
    # Tier is REASONER, not COLLECTOR (reclassified 2026-07-15): eol does not
    # enumerate an external fleet like the true collectors — it DERIVES the set
    # of in-use OSes from already-collected inventory (vSphere/Rapid7/linux/
    # windows), enriches each with an EOL date from the external endoflife.date
    # API, and materializes eol_registry. Its one external call is a cache
    # refresh; behaviorally it is analysis over collected data, so it runs in
    # the sweep's REASONER tier (after collectors + reconcilers), which is where
    # it already belongs in execution order.
    spec = AgentSpec(
        domain="eol",
        tier=Tier.REASONER,
        schedule="20 2 * * *",
        max_staleness=timedelta(hours=26),
        # TRK-359: the RUNS_EOL declaration the P5 accepted-loss record was
        # waiting for — see the block comment on _EOL_PRODUCT_NODE above.
        emits_nodes=(_EOL_PRODUCT_NODE,),
        emits_edges=_EOL_RUNS_EOL_EDGES,
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        """Derive distinct OSes from inventory and emit one eol_cycle item each.

        Stashes the derived products on ``self._derived`` for the registry write
        phase. The returned items keep the existing eol_cycle Resource/Snapshot
        shape so the generic base collect still records what was found.

        TRK-134: if inventory rows exist but NOT ONE OS string normalizes (i.e.
        ``_OS_RULES`` is under-scoped for what the fleet actually reports), that
        is a data-quality problem worth surfacing, not a silently-healthy-looking
        empty "completed" run — reported as ``status="partial"`` via
        ``CollectOutcome`` (errors + count_override, so the automatic ok/
        partial/failed mapping in ``ETLConnector.run()`` lands on "partial"
        rather than "failed": nothing actually errored, we just couldn't map
        anything). A genuinely empty fleet (no OS strings encountered at all)
        still legitimately reports "completed" with nothing to derive.
        """
        derived = self._derive_os_products()
        self._derived = derived
        if not derived:
            unmapped = getattr(self, "_unmapped_os_strings", None) or set()
            if unmapped:
                msg = (
                    f"no OS strings matched _OS_RULES; {len(unmapped)} distinct "
                    "unmapped OS string(s) seen"
                )
                logger.warning("EOLAgent: %s", msg)
                return CollectOutcome(items=[], count_override=len(unmapped), errors=[msg])
            logger.info("EOLAgent: no mappable OS strings in inventory — nothing to derive")
            return []

        _cb = {"callbacks": self.callbacks}
        cycle_cache: dict[str, list] = {}
        items = []
        # H-4: track per-product cycle-fetch success/failure through the
        # shared ReconcileScope primitive so a fetch failure can never be
        # confused with a genuine "no data" answer downstream. A product
        # whose fetch fails here gets NO "eol" key on its `derived` entry
        # (unchanged from before) -- `_write_eol_registry`/`_upsert_registry`
        # consult `scope.failed_keys` (via `_eol_fetch_failed_labels`) to
        # know that absence means "not observed this run", not "confirmed no
        # EOL data", and must never overwrite a previously-known-good
        # eol_date/pci_risk_score on that basis.
        scope = ReconcileScope(label="eol product")
        for info in derived.values():
            product, cycle, label = info["product"], info["cycle"], info["label"]
            try:
                if product not in cycle_cache:
                    cycle_cache[product] = eol_cycles_tool.invoke({"product": product}, config=_cb)
                cycles = cycle_cache[product]
            except Exception as exc:
                logger.warning("EOLAgent: failed to fetch %s: %s", product, exc)
                scope.failed(label, exc)
                continue
            scope.observed(label)
            match = self._match_cycle(cycles, cycle)
            info["eol"] = match.get("eol", "") if match else ""
            items.append(
                {
                    "name": f"{product}@{cycle}",
                    "type": "eol_cycle",
                    "data": {
                        "product": product,
                        "cycle": cycle,
                        "asset_name": info["label"],
                        "eol": info["eol"],
                        "source_count": info["count"],
                    },
                }
            )
        self._eol_fetch_failed_labels = scope.failed_keys
        return CollectOutcome(items=items, errors=scope.errors) if scope.has_failures else items

    def _detail_writers(self, scope, result):
        # Surface a registry-write failure on the CollectionRun (no silent loss)
        # via ETLConnector.run()'s _write_details.
        return [self._write_eol_registry]

    # --- OS inventory derivation ------------------------------------------

    def _derive_os_products(self) -> dict:
        """Scan inventory, normalize OS strings → distinct (product, cycle).

        Returns ``{label: {product, cycle, label, count, resource_id}}`` where
        ``resource_id`` is a representative Resource for the product.

        TRK-275/GitLab #146+#153: preferring "whichever Resource was scanned
        first" as the representative host made known hosts look like
        coverage gaps — a vSphere VM's raw Resource is vCenter-qualified
        ("name (vcenter)") and/or under a different DNS domain than the same
        physical host's Rapid7/Linux/Windows Resource, and vSphere is scanned
        first, so the representative was usually the least recognizable name.
        ``_canonical_host_resource`` reuses ``HostReconcileAgent``'s own
        identity-resolution primitives (``normalize_host`` +
        ``_SOURCE_KEYS`` anchor-priority order — no second resolver is
        written here) so that, once a host has been reconciled into a
        ``HostIdentity`` row, its highest-priority present source resource is
        preferred as the representative over an unreconciled raw candidate.
        """
        derived: dict = {}
        # TRK-134: distinct raw OS strings that were non-empty but did not
        # normalize (no _OS_RULES match) — surfaced by collect() as a
        # status="partial" signal when derived ends up empty, instead of a
        # silent status="completed" with zero rows.
        unmapped: set[str] = set()

        # Sentinel priority for a resource_id that did NOT resolve through a
        # HostIdentity row (unreconciled candidate, or hostname didn't
        # normalize) — worse (higher) than every real _SOURCE_KEYS index, so
        # any canonical resolution always outranks it.
        _fallback_priority = len(HostReconcileAgent._SOURCE_KEYS)

        def _record(norm, resource_id, hostname=None):
            product, cycle, label = norm
            canonical_rid, priority = self._canonical_host_resource(session, hostname)
            chosen_rid = canonical_rid if canonical_rid is not None else resource_id
            chosen_priority = priority if canonical_rid is not None else _fallback_priority

            entry = derived.get(label)
            if entry is None:
                derived[label] = {
                    "product": product,
                    "cycle": cycle,
                    "label": label,
                    "count": 1,
                    "resource_id": chosen_rid,
                    "_resource_priority": chosen_priority,
                }
                return

            entry["count"] += 1
            current_priority = entry.get("_resource_priority", _fallback_priority)
            if entry["resource_id"] is None:
                entry["resource_id"] = chosen_rid
                entry["_resource_priority"] = chosen_priority
            elif chosen_rid is not None and chosen_priority < current_priority:
                # A later-seen candidate resolves to a MORE-trusted canonical
                # anchor (already reconciled by HostReconcileAgent) than
                # whatever representative resource this product entry
                # currently carries — prefer it. Never demotes an existing
                # canonical pick for a lower-priority/unreconciled one.
                entry["resource_id"] = chosen_rid
                entry["_resource_priority"] = chosen_priority

        def _add(os_string, resource_id=None, hostname=None):
            norm = normalize_os_string(os_string)
            if not norm:
                if os_string:
                    logger.info("EOLAgent: unmapped OS string %r — skipped", os_string)
                    unmapped.add(str(os_string).strip())
                return
            _record(norm, resource_id, hostname)

        def _add_fp(family, product, version, os_string, resource_id=None, hostname=None):
            """Prefer the structured osFingerprint; fall back to the flat os string."""
            norm = normalize_fingerprint(family, product, version)
            if not norm:
                norm = normalize_os_string(os_string)
            if not norm:
                detail = product or os_string
                if detail:
                    logger.info("EOLAgent: unmapped Rapid7 OS %r — skipped", detail)
                    unmapped.add(str(detail).strip())
                return
            _record(norm, resource_id, hostname)

        try:
            with get_session() as session:
                # vSphere VMs — guest_full_name is the cleanest OS string;
                # guest_hostname (falling back to the VM's own name) is the
                # best available hostname for canonical-identity resolution.
                for vm in session.query(VsphereVm).all():
                    _add(vm.guest_full_name, vm.resource_id, hostname=vm.guest_hostname or vm.name)

                # Linux hosts — combine distro + version from Resource metadata.
                for res in session.query(Resource).filter_by(domain="linux").all():
                    md = res.metadata_ or {}
                    os_str = " ".join(p for p in (md.get("distro", ""), md.get("version", "")) if p)
                    _add(os_str, res.id, hostname=res.name)

                # Windows hosts — os_name + os_version from Resource metadata.
                for res in session.query(Resource).filter_by(domain="windows").all():
                    md = res.metadata_ or {}
                    os_str = " ".join(
                        p for p in (md.get("os_name", ""), md.get("os_version", "")) if p
                    )
                    _add(os_str, res.id, hostname=res.name)

                # Rapid7 assets — PRIMARY OS source (MR6). The relational
                # ``r7_assets`` rows carry the structured osFingerprint fields
                # (os_family/os_product/os_version), which are far cleaner than
                # the old (and previously-empty) Resource.metadata os string.
                for a in session.query(R7Asset).all():
                    _add_fp(
                        a.os_family,
                        a.os_product,
                        a.os_version,
                        a.os,
                        a.resource_id,
                        hostname=a.hostname,
                    )
        except Exception as exc:
            logger.warning("EOLAgent: failed to scan OS inventory: %s", exc)
            raise

        self._unmapped_os_strings = unmapped
        return derived

    @staticmethod
    def _canonical_host_resource(session, hostname: str | None) -> tuple:
        """Resolve *hostname* to its canonical, host-reconciled Resource id.

        TRK-275/GitLab #146+#153. Reuses ``HostReconcileAgent``'s own
        identity-resolution primitives — ``normalize_host()``
        (``tools/hostmatch.py``, the same function ``host_reconcile.py``
        keys ``host_identities`` on) plus its ``_SOURCE_KEYS`` anchor-priority
        order — instead of writing a second host-identity resolver. That order
        was originally shared with ``HostReconcileAgent._anchor_source``, which
        picked the stable representative endpoint for that agent's own
        IS_SAME_AS edges; those edges and that helper were deleted in P5
        (identity now belongs solely to ``graph_phase3``), so ``_SOURCE_KEYS``
        itself is the surviving definition of source trust order and this is now
        its primary consumer.

        Returns ``(resource_id, priority)`` where ``priority`` is the index
        into ``HostReconcileAgent._SOURCE_KEYS`` of whichever source
        resource_id was picked (lower = more trusted/canonical). Returns
        ``(None, len(_SOURCE_KEYS))`` when *hostname* does not normalize to
        anything, or no ``HostIdentity`` row exists yet for it (host_reconcile
        runs on its own 30-minute schedule — a brand-new host may not have
        been reconciled yet; the caller falls back to the raw per-domain
        Resource id in that case).
        """
        fallback = (None, len(HostReconcileAgent._SOURCE_KEYS))
        try:
            short = normalize_host(hostname or "")
            if not short:
                return fallback
            identity = session.query(HostIdentity).filter_by(short_hostname=short).first()
        except Exception as exc:
            # Identity resolution is a best-effort enhancement over the raw
            # per-domain Resource id, not a hard requirement of EOL derivation
            # -- a host_identities query error here must degrade to the old
            # raw-Resource behavior, not fail the whole EOL collection run.
            logger.warning(
                "EOLAgent: canonical host resolution failed for %r, falling back "
                "to raw resource id: %s",
                hostname,
                exc,
            )
            return fallback
        if identity is None:
            return fallback
        for i, (key, _label) in enumerate(HostReconcileAgent._SOURCE_KEYS):
            rid = getattr(identity, key, None)
            if rid:
                return rid, i
        return fallback

    @staticmethod
    def _match_cycle(cycles, cycle: str):
        """Find the cycle dict whose ``cycle`` matches (string-normalized)."""
        if not isinstance(cycles, list):
            return None
        want = str(cycle).strip().lower()
        for c in cycles:
            if str(c.get("cycle", "")).strip().lower() == want:
                return c
        return None

    # --- registry write ----------------------------------------------------

    @staticmethod
    def _parse_eol(eol_value):
        """endoflife.date 'eol' is an ISO date string, or a bool (no fixed date)."""
        if not eol_value or isinstance(eol_value, bool):
            return None
        try:
            return datetime.fromisoformat(str(eol_value)).replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _pci_risk_score(eol_dt, now) -> int:
        """Score from EOL proximity: past→90, <90d→70, <1yr→40, else 10.

        GitLab #186: an unknown/unset ``eol_dt`` used to score 10 — the
        LOWEST value on this 0-100 (higher = more urgent) scale — which
        deprioritized "we don't even know when this goes EOL" below "goes
        EOL in 300 days". Not knowing the EOL date is at least as urgent as
        a confirmed past-EOL asset (it needs investigation before it can be
        ruled out), so it scores the same as the past-EOL tier: 90.
        """
        if eol_dt is None:
            return 90
        days = (eol_dt - now).days
        if days < 0:
            return 90
        if days < 90:
            return 70
        if days < 365:
            return 40
        return 10

    def _write_eol_registry(self) -> None:
        derived = getattr(self, "_derived", None)
        if not derived:
            return

        # H-4: labels whose endoflife.date cycle fetch FAILED this run (set
        # by collect() via ReconcileScope). For these, `info` carries no
        # "eol" key at all -- writing that through as eol_date=None would
        # fabricate "no EOL data" out of a fetch failure and force
        # pci_risk_score to the max-urgency 90, silently destroying whatever
        # was already known. Only a label whose fetch actually succeeded may
        # overwrite an existing row's eol_date/pci_risk_score.
        fetch_failed = getattr(self, "_eol_fetch_failed_labels", frozenset())

        now = datetime.now(UTC)
        written = 0
        with get_session() as session:
            for label, info in derived.items():
                self._upsert_registry(
                    session,
                    label,
                    info.get("resource_id"),
                    info,
                    now,
                    fetch_failed=label in fetch_failed,
                )
                written += 1
            session.commit()
        logger.info("EOLAgent: eol_registry upserted %d derived products", written)

    def _upsert_registry(
        self, session, asset_name, resource_id, info, now, *, fetch_failed: bool = False
    ) -> None:
        """Upsert by ``asset_name`` so derived + manual entries merge.

        ``resource_id`` is NOT NULL in the schema. If derivation found no
        representative Resource and no existing row supplies one, we attach a
        small ``domain="eol"`` product Resource (mirrors ``add_eol_product``).

        H-4: when ``fetch_failed`` is True (this run's endoflife.date cycle
        fetch for this product failed -- an outage, not a "no data" answer)
        AND a row already exists, ``eol_date``/``pci_risk_score`` are left
        completely untouched -- a transient outage must never overwrite a
        previously-known-good value. The resource association and an unset
        migration suggestion are still safe to refresh, since neither
        depends on this run's cycle fetch. There is nothing known-good to
        preserve for a brand-new row, so that case falls through to the
        normal (possibly "unknown" -> None/90) write, matching prior
        first-observation behavior.
        """
        existing = session.query(EolRegistry).filter_by(asset_name=asset_name).first()

        if fetch_failed and existing is not None:
            if resource_id is not None:
                existing.resource_id = resource_id
            if not existing.migration_path:
                existing.migration_path = _suggest_migration(asset_name)
            return

        eol_dt = self._parse_eol(info.get("eol"))
        score = self._pci_risk_score(eol_dt, now)

        if existing is not None:
            existing.eol_date = eol_dt
            existing.pci_risk_score = score
            existing.last_updated = now
            if resource_id is not None:
                existing.resource_id = resource_id
            # Auto-suggest only when the operator has not already set a path.
            if not existing.migration_path:
                existing.migration_path = _suggest_migration(asset_name)
            return

        if resource_id is None:
            from infra_brain.api._seeding import upsert_resource

            res = upsert_resource(
                session,
                name=asset_name,
                domain="eol",
                resource_type="product",
                source=type(self).__name__,
            )
            resource_id = res.id

        session.add(
            EolRegistry(
                resource_id=resource_id,
                asset_name=asset_name,
                eol_date=eol_dt,
                pci_risk_score=score,
                migration_path=_suggest_migration(asset_name),
                last_updated=now,
            )
        )
