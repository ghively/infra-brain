"""One-off backfill for GitLab issue #122: vuln_queue severity mislabeled.

Root cause (see agents/vuln.py::_write_vuln_queue): the Rapid7 asset-vuln item
that fed vuln_queue.severity never carries a real severity/CVSS score — that
data only exists on the separate vuln *definition* endpoint, enriched into
``r7_vulnerabilities`` by VulnCveBridge. Every ``vuln_queue`` row therefore got
``severity="low"`` regardless of the finding's actual CVSS. The fix in
``_write_vuln_queue`` re-derives severity from ``r7_vulnerabilities`` going
forward, and — because ``_upsert_vuln_row`` unconditionally refreshes
severity on every re-scan — a fresh agent run naturally corrects any row for
an asset still inside the bounded top-N cap (``RAPID7_VULN_ASSET_CAP``, ~750
of ~2586 assets). This script corrects the REST: any existing ``vuln_queue``
row whose CVE is already enriched in ``r7_vulnerabilities`` (via the
``r7_vuln_cves`` bridge), regardless of whether its asset falls inside this
run's cap — a pure internal DB correction, no Rapid7 calls, no external
writes.

A row whose CVE has no enrichment yet (enrichment lags collection — see
docs/agents/vuln.md) is left untouched: "low" is its correct "not yet
enriched" placeholder, not a bug.

READ FIRST: run with --dry-run (default) to see the per-row report; pass
--apply to actually write.
Usage:  python scripts/backfill_vuln_queue_severity.py [--apply]
"""

import sys
from collections import Counter

from infra_brain.agents.vuln import VulnAgent
from infra_brain.db import severity as sev
from infra_brain.db.models import R7Vulnerability, R7VulnCve, VulnQueueItem
from infra_brain.db.session import get_session


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[backfill-vuln-severity] mode={mode}")

    changed = 0
    unchanged = 0
    not_enriched = 0
    band_changes: Counter = Counter()

    with get_session() as session:
        # Walk the same clean join the production readers use:
        # vuln_queue.cve_id -> r7_vuln_cves.cve_id -> r7_vuln_id ->
        # r7_vulnerabilities (severity, cvss_v3_score). A CVE can map to
        # multiple slugs (rollup patches); pick the worst (highest-ranked)
        # band across all of them, matching the run-time enrichment-priority
        # convention used elsewhere in this agent.
        enrichment: dict[str, tuple] = {}
        rows = (
            session.query(
                R7VulnCve.cve_id,
                R7Vulnerability.severity,
                R7Vulnerability.cvss_v3_score,
            )
            .join(R7Vulnerability, R7Vulnerability.r7_vuln_id == R7VulnCve.r7_vuln_id)
            .all()
        )
        for cve_id, enr_severity, enr_cvss in rows:
            band = VulnAgent._severity_band(enr_severity, enr_cvss)
            prev = enrichment.get(cve_id)
            if prev is None or sev.rank(band) > sev.rank(prev[0]):
                enrichment[cve_id] = (band, enr_severity, enr_cvss)

        print(f"[backfill-vuln-severity] {len(enrichment)} distinct enriched CVEs found")

        for item in session.query(VulnQueueItem).all():
            enr = enrichment.get(item.cve_id)
            if enr is None:
                not_enriched += 1
                continue
            new_band = enr[0]
            if item.severity == new_band:
                unchanged += 1
                continue
            band_changes[(item.severity, new_band)] += 1
            if apply:
                item.severity = new_band
            changed += 1

        print(
            f"[backfill-vuln-severity] rows: {changed} would change, "
            f"{unchanged} already correct, {not_enriched} not yet enriched (left as-is)"
        )
        for (old, new), count in sorted(band_changes.items()):
            print(f"  {old!r} -> {new!r}: {count} row(s)")

        if apply:
            session.commit()
            print(f"[backfill-vuln-severity] committed — {changed} row(s) corrected")
        else:
            session.rollback()
            print(f"[backfill-vuln-severity] dry-run — would correct {changed} row(s) (rolled back)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
