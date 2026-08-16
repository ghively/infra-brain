"""CI lint (F-007): no agent may swallow an exception and return an empty value
without recording the failure.

Rule: inside any ``except`` handler in ``src/infra_brain/agents/*.py``, a
``return`` whose value is an empty list/dict literal is forbidden UNLESS the
handler also records the failure — i.e. it contains a ``raise``, an
``.append(...)`` call (the errors-list contract from CollectOutcome), or a
``.failed(...)`` call (``ReconcileScope.failed``, etl/base.py — which appends
to the scope's own error list AND narrows the destructive-reconciliation
scope, so it is a strictly stronger record than a bare append).

This runs as part of pytest, so the existing CI test job is the gate — no
.gitlab-ci.yml change is needed.
"""

import ast
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "infra_brain" / "agents"

# (filename, lineno) pairs explicitly allowed. Keep EMPTY — add an entry only
# with a review-approved justification comment next to it.
#
# Item 2.1 (F-007) fixes the collect()-phase swallow-site inventory named in
# wave-2-plan.md: cicd.py, cloud.py, net.py, octopus.py, vsphere.py, vuln.py
# (asset-fetch block only), iac.py, discovery.py, drift_learning.py, eol.py,
# fleet_health.py, windows.py. The sites below are pre-existing swallows this
# lint also catches but that are OUT OF SCOPE for 2.1 — each is a bounded,
# best-effort per-item lookup (not a collect()-phase total/partial failure
# path) or is explicitly assigned to a later wave item. Tracked for follow-up
# rather than silently left unguarded.
ALLOWLIST: set[tuple[str, int]] = {
    # Per-project inventory file read failure — not in the F-007 site
    # inventory for Item 2.1; candidate for a follow-up finding.
    ("inventory_reconcile.py", 60),
    # Invalid-subnet-string parse fallback in passive discovery; netdiscovery.py's
    # timeout-guard bypass is Item 2.6 scope, this site is unrelated to it.
    # Line shifted by +22 after MR-C added the collection_disabled_domains
    # (AA-R-11/12) skip check + _mark_run_skipped() to run(), then +2 after
    # MR-K (docs-reconcile) expanded the stale OUI-comparison comment (AA-D-26)
    # ahead of this site in the same file, then +1 after the _is_fragile
    # docstring update (Phase 1 Task 3 review nit).
    # ... then +5 after the AgentSpec declaration (Phase 1 Task 5, TRK-047).
    # ... then +4 after Phase 2 Task 1 added the sweep_id kwarg to run()'s
    # signature (sweep groundwork).
    # ... then +17 after TRK-239 added the partial-status mapping to run() and
    # the _sweep_targets() scope filter ahead of this site in the same file.
    # ... then +8 after GitLab #148 added the detail_rows_written comment +
    # assignment to run()'s finalize block ahead of this site in the same file.
    # ... then +33 after GitLab #151's hostname cheap-win (2026-07-30) rewrote
    # _tier0_passive()/_ptr_sweep() to carry resolved hostnames instead of
    # discarding them, adding code ahead of this site in the same file.
    # ... then +1 after TRK-280/281's nmap timeout/rate tuning (2026-07-30)
    # added a line ahead of this site in the same file.
    # ... then -2 after the TRK-280/281 Tier 2 budget/resilience follow-up
    # (2026-07-30) removed a line ahead of this site in the same file.
    # ... then +18 after the GitLab #178 chunk/subnet-order rotation fix
    # (2026-08-01) added the run_ordinal derivation block to run() and
    # expanded the Tier 1 _call_with_timeout invocation onto multiple lines,
    # both ahead of this site in the same file.
    # ... then +72 after the Tailscale mesh-peer discovery source
    # (2026-08-02) added _tailscale_peers() ahead of this site in the same
    # file (see the new allowlist entry directly below for that site's own
    # degrade-to-{} handler).
    # ... then +12 after the audit's HIGH-severity fix (2026-08-06) made
    # NetDiscoveryAgent actually set info["discovery_tier"] instead of
    # always defaulting to "passive", adding lines ahead of this site in the
    # same file.
    # ... then +26 after M-1 (run()-override hardening) wrapped run() in the
    # shared ETLConnector.run_row_guard and moved the Tier 0-3 body into
    # _run_tiers(), both ahead of this site in the same file.
    # ... then +12 after the M-2 fix (F-007 per-item SAVEPOINT skip loops,
    # 2026-08-10) added the _persist_skip_errors reset in _run_tiers() and
    # the errors.extend(...) fold-in at the _persist() call site, both ahead
    # of this site in the same file. This site itself is UNRELATED to M-2 —
    # it is the pre-existing bounded/best-effort _ptr_sweep() degrade, not a
    # collect()-phase total/partial failure path.
    ("netdiscovery.py", 730),
    # Tailscale mesh-peer discovery (_tailscale_peers, 2026-08-02): a bounded,
    # best-effort passive Tier-0 source, same class as the site above — a
    # missing/absent tailscaled degrades this ONE source to {} (the caller
    # still has R7Asset/VsphereVm/HostIdentity/PTR-sweep sources), not the
    # collect()-phase total/partial failure path. Logged via logger.debug()
    # with the exception detail.
    # ... then +12, same discovery_tier fix as the entry above.
    # ... then +26 after M-1 (run()-override hardening) wrapped run() in the
    # shared ETLConnector.run_row_guard and moved the Tier 0-3 body into
    # _run_tiers(), both ahead of this site in the same file.
    # ... then +12 after the M-2 fix (F-007 per-item SAVEPOINT skip loops,
    # 2026-08-10), same shift as the entry above — this site is likewise
    # UNRELATED to M-2.
    ("netdiscovery.py", 699),
    # vuln.py's MR7 vuln-detail/solution enrichment helpers (bounded,
    # best-effort per-CVE-slug lookups already logged with context) — Item
    # 2.1 Step 10 only touches the asset-fetch block earlier in collect().
    # Line numbers reflect BOTH MR-D's per-asset SAVEPOINT guard +
    # written/failed counters (DL-C-5/DL-C-6) AND MR-E's FE-9 stale-row
    # reconciliation pass to _write_vuln_queue, combined in this merge —
    # verified by running the lint test against the actual merged file.
    ("vuln.py", 732),
    ("vuln.py", 741),
    ("vuln.py", 750),
    # vuln_cve.py mirrors the same bounded enrichment-helper pattern as
    # vuln.py above; same rationale.
    # L-3 fix (review-remediation wave 2): _fetch_solution_ids' bare
    # ``return []`` (previously line 304) is GONE -- it now returns
    # ``(ids, ok)`` so the caller can tell a genuinely-empty result apart
    # from a swallowed fetch failure (a transient outage must not erase a
    # known fix_available=True). That tuple return is not an empty literal,
    # so it no longer needs (or matches) an allowlist entry. The docstring
    # added alongside it shifted the two sibling bare-``{}`` returns below by
    # +12 lines (295->307, 313->330).
    ("vuln_cve.py", 307),
    ("vuln_cve.py", 330),
    # NOTE: LinuxAgent._merge_enrichment_facts()'s _safe_invoke() helper was
    # allowlisted here from MR-J (INV-1) through TRK-031 on the rationale that a
    # per-probe enrichment failure is "bounded and best-effort", so losing it
    # was harmless. H-5 disproved that: the swallowed probe left its fact key
    # ABSENT, which the delete-then-reinsert detail writers could not tell apart
    # from "this host genuinely has no rows", so one SSH blip deleted every
    # stored port/user/cron/posture row — and the emptied linux_ports table
    # disarmed new-listening-port drift detection permanently. The handler now
    # records into a ReconcileScope (which both scopes the deletes and feeds
    # CollectOutcome.errors), so it satisfies the rule outright and needs no
    # exemption. Do not re-add this entry.
}


def _is_empty_literal(node) -> bool:
    return (isinstance(node, ast.List) and not node.elts) or (
        isinstance(node, ast.Dict) and not node.keys
    )


def _handler_records_failure(handler: ast.ExceptHandler) -> bool:
    for inner in ast.walk(handler):
        if isinstance(inner, ast.Raise):
            return True
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in ("append", "failed")
        ):
            return True
    return False


def test_no_unrecorded_swallow_and_return_empty_in_agents():
    violations: list[str] = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if _handler_records_failure(node):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and _is_empty_literal(inner.value):
                    if (path.name, inner.lineno) not in ALLOWLIST:
                        violations.append(f"{path.name}:{inner.lineno}")
    assert violations == [], (
        "silent swallow-and-return-empty (F-007) — record the error in a "
        "CollectOutcome errors list or re-raise. Sites: " + ", ".join(violations)
    )
