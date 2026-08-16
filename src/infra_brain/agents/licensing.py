"""LicensingAgent — reconcile commercial-software entitlement vs. installed base.

GitLab issue #97 ("license compliance"): distinct from EOLAgent (eol.py,
end-of-life dates) and VulnAgent (vuln.py, CVE exposure). Generalizes the
existing per-vCenter ``VsphereLicense`` total/used pattern (vsphere.py) to any
commercial software product visible in the Windows/Rapid7/Linux installed-
software inventory (``WindowsSoftware``/``R7Software``/``LinuxPackage`` —
db/models/os_inventory.py, rapid7.py).

WHERE THE INSTALLED BASE COMES FROM (graph-first P4, 2026-08-12)
----------------------------------------------------------------
This agent used to read ``HAS_SOFTWARE`` rows out of the legacy
``resource_relationships`` store. It now reads the three inventory FACT
TABLES directly. ``HAS_SOFTWARE`` is a **containment** fact — "this host has
this package on it" — not a relationship between two independently-referrable
entities, so per §3.1 of ``docs/decisions/2026-08-10-graph-edge-authority-spec.md``
it was never migrated into ``graph_edges`` and never will be (the P3 backfill
migration ``8965b6329b94`` lists it explicitly in ``CONTAINMENT_TYPES``).
Re-pointing this reader at ``graph_edges`` would therefore have read an empty
table; the fact tables are the correct replacement.

Two consequences worth knowing:

  * The counts are now **first-hand**. Previously an install was only counted
    once ``graph_maintenance``'s 2-hourly derivation pass had materialised a
    ``software`` Resource for it and emitted an edge, so a freshly-collected
    host was invisible to license reconciliation for up to two hours (longer
    when the pass was gated off by its source-freshness check). The fact
    tables are written by the collectors themselves.
  * It no longer matters whether ``graph_maintenance`` runs at all. The
    equivalence is pinned by ``tests/agents/test_licensing.py``, which
    reproduces the deriver's block-6 logic and asserts the fact-table read
    yields the identical ``{product -> {host resource_id}}`` mapping.

Per the issue, this starts as the lowest-collection-complexity option: a pure
RECONCILIATION PASS over data infra-brain already has, not a new external API
poll. The operator seeds entitlement ground truth by hand into
``SoftwareLicense`` rows (db/models/licensing.py — product, license_type,
seats/cores entitled, expiry); this agent counts the *installed* base for
each entitled product and reports over-/under-entitlement.

No ``SoftwareLicense`` rows seeded yet → ``collect()`` raises
``CollectorSkipped``. There is nothing to reconcile AGAINST, which is an absent
dependency rather than a clean result of zero findings. It previously returned
an empty list, recording status="completed" with 0 rows — the R3 monitor
correctly reads that as "silently empty?" and had escalated it 200x in a row
for a reconciliation nobody had given any input to. Seeding one entitlement row
clears the skip by itself. A real error reading inventory (DB failure) is still
logged and re-raised, same as EOLAgent's ``_derive_os_products``, so
``ETLConnector.run()`` marks the CollectionRun ``failed``.

Findings surface as ``ComplianceViolation`` rows (compliance.py's model —
this agent writes to it directly, it does not modify compliance.py) so
license-compliance gaps show up next to every other policy-as-code finding
in the existing dashboard/API surface, one row per (rule, product):
  * ``license_over_entitlement`` — installed count exceeds what was bought.
  * ``license_under_entitlement`` — bought more than is installed (cost lead).
A product that comes back in compliance clears any previously open violation
for it, mirroring ``ComplianceAgent._reconcile``'s resolve-what-cleared step.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from infra_brain.db.models import (
    ComplianceViolation,
    LinuxHost,
    LinuxPackage,
    R7Asset,
    R7Software,
    SoftwareLicense,
    WindowsSoftware,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# Severity bands for an over-entitlement finding, by how far installed count
# exceeds what was bought (as a fraction of entitled capacity). Under-
# entitlement (unused seats) is always "low" — it's a cost-optimization
# signal, not a compliance risk.
_OVER_ENTITLEMENT_SEVERITY_RATIO_CRITICAL = 0.5  # installed >= 150% of entitled
_OVER_ENTITLEMENT_SEVERITY_RATIO_HIGH = 0.1  # installed >= 110% of entitled


class LicensingAgent(ETLConnector):
    # REASONER, not COLLECTOR (same rationale as EOLAgent): this agent does
    # not enumerate an external fleet — it reconciles already-collected
    # HAS_SOFTWARE inventory against manually-seeded entitlement rows.
    spec = AgentSpec(
        domain="licensing",
        tier=Tier.REASONER,
        schedule="50 4 * * *",
        max_staleness=timedelta(hours=26),
        skip_hook=True,
        # DELIBERATELY NOT retired (2026-08-12), though "licensing" reads
        # enterprise. It talks to no vendor system at all: it reconciles the
        # installed base against the `software_licenses` table, which an
        # operator seeds by hand. That is local data, and a home lab with any
        # paid software can legitimately use it. Retiring a collector because
        # its NAME sounds corporate — rather than because its upstream does not
        # exist — is exactly the mistake to avoid here. It already self-skips
        # cleanly while the table is empty.
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        with get_session() as session:
            entitlements = session.query(SoftwareLicense).all()

        if not entitlements:
            # Self-skip, not an empty success. This agent reconciles the
            # installed base against operator-seeded entitlement ground truth;
            # with no SoftwareLicense rows there is nothing to reconcile
            # AGAINST, which is an absent dependency rather than a clean result
            # of zero findings. Returning [] recorded status="completed" with 0
            # rows, and the R3 completeness monitor correctly read that as
            # "silently empty?" — escalating 200x in a row for a reconciliation
            # nobody had given it any input for. Seeding a single row clears
            # this by itself.
            self._findings = []
            raise CollectorSkipped(
                "no SoftwareLicense entitlement rows seeded — seed entitlements to enable "
                "license reconciliation"
            )

        try:
            with get_session() as session:
                installed = self._installed_host_sets_by_product(session)
        except Exception:
            logger.warning("LicensingAgent: failed to read installed-software inventory", exc_info=True)
            raise

        now = datetime.now(UTC)
        findings = []
        items = []
        for ent in entitlements:
            installed_count = self._match_installed_count(ent.product, installed)
            finding = self._reconcile_one(ent, installed_count, now)
            findings.append(finding)
            items.append(
                {
                    "name": f"{ent.product}@{ent.license_type}",
                    "type": "license_reconciliation",
                    "data": {
                        "product": ent.product,
                        "license_type": ent.license_type,
                        "seats_entitled": ent.seats_entitled,
                        "cores_entitled": ent.cores_entitled,
                        "installed_count": installed_count,
                        "status": finding["status"],
                    },
                }
            )
        self._findings = findings
        return items

    def _detail_writers(self, scope, result):
        return [self._write_license_violations]

    # --- installed-base reconciliation -------------------------------------

    @staticmethod
    def _installed_host_sets_by_product(session) -> dict[str, set]:
        """Return ``{product_lower: {installed host resource_id, ...}}``.

        Reads the three installed-software fact tables directly — the exact
        sources ``graph_maintenance``'s HAS_SOFTWARE block reads before it
        derives anything:

          * ``WindowsSoftware.resource_id`` / ``.name``
          * ``R7Software.product`` via ``R7Asset.resource_id``
          * ``LinuxPackage.name`` via ``LinuxHost.resource_id``

        The keys are the raw product strings, lower-cased and trimmed, which
        is exactly what the deriver stamped into each software Resource's
        ``metadata["product"]`` and what this method used to read back out of
        it. Version is deliberately NOT part of the key: an entitlement is
        seeded per product, and the old edge-reading path likewise collapsed
        every ``<product>@<version>`` node onto its bare ``product``.

        See the module docstring for why this is the fact tables rather than
        ``graph_edges`` — HAS_SOFTWARE is containment, and containment facts
        were never migrated into the graph store.
        """
        hosts_by_product: dict[str, set] = defaultdict(set)

        def _add(host_resource_id, product) -> None:
            # Mirrors the deriver's own guards: it skips rows with no product
            # string and requires a non-NULL host resource_id.
            if host_resource_id is None or not product:
                return
            key = str(product).strip().lower()
            if key:
                hosts_by_product[key].add(host_resource_id)

        for host_rid, name in (
            session.query(WindowsSoftware.resource_id, WindowsSoftware.name)
            .filter(WindowsSoftware.resource_id.isnot(None))
            .all()
        ):
            _add(host_rid, name)

        for host_rid, product in (
            session.query(R7Asset.resource_id, R7Software.product)
            .join(R7Software, R7Software.asset_id == R7Asset.id)
            .filter(R7Asset.resource_id.isnot(None))
            .all()
        ):
            _add(host_rid, product)

        for host_rid, name in (
            session.query(LinuxHost.resource_id, LinuxPackage.name)
            .join(LinuxPackage, LinuxPackage.host_id == LinuxHost.id)
            .filter(LinuxHost.resource_id.isnot(None))
            .all()
        ):
            _add(host_rid, name)

        return dict(hosts_by_product)

    @staticmethod
    def _match_installed_count(product: str, host_sets: dict[str, set]) -> int:
        """Match an entitlement's product name against installed software.

        Exact case-insensitive match first; falls back to a substring match
        in either direction (mirrors EOLAgent's ``_suggest_migration``
        lowercase-substring approach) so e.g. an entitlement seeded as
        "Microsoft SQL Server" still matches an installed
        "Microsoft SQL Server 2019 Standard" software Resource. Substring
        matches are unioned by host so a host running two matching product
        strings is not double-counted.
        """
        want = product.strip().lower()
        exact = host_sets.get(want)
        if exact is not None:
            return len(exact)
        matched: set = set()
        for key, hosts in host_sets.items():
            if want in key or key in want:
                matched |= hosts
        return len(matched)

    # --- per-entitlement reconciliation -------------------------------------

    @staticmethod
    def _reconcile_one(ent: SoftwareLicense, installed_count: int, now: datetime) -> dict:
        """Compare one entitlement row against its installed count.

        Returns a dict always containing ``status``; ``rule``/``severity``/
        ``detail`` are only meaningful when ``status`` is
        "over_entitlement"/"under_entitlement" (the two states that produce a
        ComplianceViolation row).
        """
        if ent.seats_entitled is not None:
            capacity, metric = ent.seats_entitled, "seats"
        elif ent.cores_entitled is not None:
            capacity, metric = ent.cores_entitled, "cores"
        else:
            # No fixed cap (e.g. site license) — nothing to reconcile against.
            return {"status": "unmetered", "product": ent.product}

        if installed_count > capacity:
            over_by = installed_count - capacity
            ratio = over_by / capacity if capacity else 1.0
            if ratio >= _OVER_ENTITLEMENT_SEVERITY_RATIO_CRITICAL:
                severity = "critical"
            elif ratio >= _OVER_ENTITLEMENT_SEVERITY_RATIO_HIGH:
                severity = "high"
            else:
                severity = "medium"
            return {
                "status": "over_entitlement",
                "product": ent.product,
                "rule": "license_over_entitlement",
                "severity": severity,
                "detail": (
                    f"{installed_count} installs of {ent.product} exceed entitled "
                    f"{capacity} {metric} (over by {over_by})"
                ),
            }
        if installed_count < capacity:
            return {
                "status": "under_entitlement",
                "product": ent.product,
                "rule": "license_under_entitlement",
                "severity": "low",
                "detail": (
                    f"only {installed_count} of {capacity} entitled {metric} of "
                    f"{ent.product} are installed (unused: {capacity - installed_count})"
                ),
            }
        return {"status": "in_compliance", "product": ent.product}

    # --- ComplianceViolation write -------------------------------------------

    def _write_license_violations(self) -> None:
        findings = getattr(self, "_findings", None)
        if not findings:
            return

        now = datetime.now(UTC)
        current_keys = {
            (f["rule"], f["product"])
            for f in findings
            if f["status"] in ("over_entitlement", "under_entitlement")
        }

        with get_session() as session:
            existing = {
                (cv.rule, cv.host): cv
                for cv in session.query(ComplianceViolation)
                .filter(
                    ComplianceViolation.rule.in_(
                        ("license_over_entitlement", "license_under_entitlement")
                    ),
                    ComplianceViolation.status == "open",
                )
                .all()
            }

            for f in findings:
                if f["status"] not in ("over_entitlement", "under_entitlement"):
                    continue
                key = (f["rule"], f["product"])
                cv = existing.get(key)
                if cv is not None:
                    cv.detail = f["detail"]
                    cv.severity = f["severity"]
                    cv.detected_at = now
                else:
                    session.add(
                        ComplianceViolation(
                            rule=f["rule"],
                            host=f["product"],
                            severity=f["severity"],
                            detail=f["detail"],
                            status="open",
                            detected_at=now,
                        )
                    )

            # Resolve violations for products that cleared (now in compliance,
            # unmetered, or no longer entitled at all).
            cleared = [(key, cv) for key, cv in existing.items() if key not in current_keys]
            for (rule, host), _cv in cleared:
                session.query(ComplianceViolation).filter_by(
                    rule=rule, host=host, status="resolved"
                ).delete(synchronize_session=False)
            session.flush()
            for _key, cv in cleared:
                cv.status = "resolved"

            session.commit()
        logger.info(
            "LicensingAgent: %d entitlement(s) reconciled, %d violation(s) open",
            len(findings),
            len(current_keys),
        )
