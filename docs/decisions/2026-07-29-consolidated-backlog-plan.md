# ADR 2026-07-29 — Consolidated Backlog Plan (supersedes the sequencing in `2026-07-29-implementation-plan.md`)

**Status:** EXECUTED (updated 2026-07-30) — this branch was never itself merged (docs-only
oversight, not a rejection), but every phase it planned landed via other branches:
Phase A (`wt/phase-a/trk269-*`, `trk270-*`, `trk278-*`), Phase B
(`wt/phase-b/T1-bulk-rootcause-notes`, `T2-trk247-runtime-guard`,
`T3-fleet-health-count-override`, `T4-mcp-pagination`), and Phase D
(`wt/phase-d/T1-eol-registry-fix`, `T2-instinct-sync`, `T3-notification-audit-trail`) are all
confirmed ancestors of `master` as of 2026-07-30. Merging this document now purely to
preserve the decision record — the original **PROPOSED** framing below is left as written.
**Author:** brainstorming session, 2026-07-29, with the maintainer.
**Supersedes:** `docs/decisions/2026-07-29-implementation-plan.md`'s Phase 0-4 ordering. That
document's technical analysis (blast-radius writeups for the Phase 2 MCP tools, the settled
premises about TRK-247) is still correct and is referenced, not repeated, below. What changed
is scope and sequencing: that plan only covered the TRK-247..259 audit batch. This one merges
it with a second, larger "2026-07-29 infra-ops triage" batch (GitLab #140, #145-157 /
TRK-268-278) that the prior plan predates entirely, and folds in three decisions made by the maintainer
during this session that were previously blocking (§2).

**Inputs:** `docs/TRACKER.md` (through TRK-279), `docs/HANDOFF.md`, `docs/decisions/2026-07-29-implementation-plan.md`,
live GitLab issue state (`glab issue view`/`glab issue list`, re-verified during this session —
not assumed from any doc), and direct code checks (`grep` confirming which Phase 2/3 MCP tools
from the prior ADR are actually present in `src/infra_brain/mcp_server.py`).

---

## 1. What changed since the last plan (verify-before-plan findings)

Re-checked against live state rather than trusting either prior doc:

- **Already shipped** since the prior ADR was written: Phase 0 (TRK-247 decision, TRK-252
  observation, TRK-258(1) healthcheck), Phase 1 (TRK-248/250/251/253/254/257), and Phase 2.1
  (`get_manual_writes()`), 2.2 (`has_note` filter on `get_drift_events`), and 2.4 (`PATCH
  /api/dashboard/mcp-keys/{key_id}`) — confirmed present in `mcp_server.py`/the dashboard API
  by direct grep, not by trusting a status line.
- **Not shipped, despite the decision being made:** Phase 2.3
  (`record_rootcause_notes_bulk()` — absent from `mcp_server.py`) and Phase 3.1 (TRK-247's
  runtime guard — no `authored_by="direct:...`-shaped code found).
- **GitLab #125, #126, #127, #128** (relationship-graph Phase 2/3, role-tagging pipeline,
  `get_host_context`) are **already closed** — confirmed via `glab issue view` state, not
  assumed from the roadmap count in HANDOFF.md, which still listed them as part of an
  untouched 12-issue `roadmap-agents` batch. The real open `roadmap-agents` count is 10
  (#94-103), plus #104/#105 which are scoping-only/blocked.
- **New since the prior ADR:** the second triage batch, TRK-268 through TRK-278 (GitLab #140,
  #145-157), entirely unaccounted for in the prior document.
- **Newly unblocked this session** — three decisions made by the maintainer (§2) that move TRK-275,
  TRK-276, TRK-277 from "decision-gated" to "ready to schedule."
- **Stale tracker row found and left for correction, not re-planned:** TRK-267/GitLab #136
  (`sla_due` fix) still reads "DRAFTED, not yet merged," but the fix already landed via a
  different commit (`927cfa0`, confirmed an ancestor of `master`). This is a tracker
  housekeeping fix, not a build item — folded into Phase A below as a zero-risk docs task.

---

## 2. Decisions made this session (previously blocking)

| Row | Decision | What it unblocks |
|---|---|---|
| TRK-275 / GitLab #146+#153 | `pci_risk_score`'s 0-100 scale in code is correct and internally consistent (confirmed by direct code read of `agents/eol.py::_pci_risk_score`); **fix docs/consumers to match the code**, not the reverse. Separately, normalize the EOL registry's identity key from vSphere VM name to canonical host identity. | Phase D.1 |
| TRK-276 / GitLab #154 | **infra-brain's DB-backed instinct store is authoritative.** Import/sync infra-ops's git-committed YAML instinct ledger into it (direction: YAML → DB), not the reverse. | Phase D.2 |
| TRK-277 / GitLab #157 | Jira is **not** currently meant to be configured on deploy-host-01 — this is a code gap, not a config gap. `NotificationAgent` must record an internal ack/audit trail even when it can't deliver to Jira, instead of silently no-opping the entire sweep. | Phase D.3 |

Still correctly excluded from this plan (external/operator-gated, unchanged from the prior
ADR, do not re-litigate): `OPS_WEBHOOK_URL` provisioning (TRK-135, still the single
highest-leverage non-code item), Bedrock access hold on all reasoner-tier LLM work, TRK-099
fleet SSH, TRK-249 (needs the maintainer to inspect the live GitLab token), TRK-259 (disk trend, watch
only), and `fix/6.1-mig-b6-flip` (the maintainer confirmed: keep holding, still soaking).

---

## 3. Phase spine

Ordering rationale: (A) pure risk-reduction — nothing here can go wrong in an interesting
way, dispatch immediately in parallel; (B) finishes work that already has one round of design
review behind it (the existing MCC ADR) rather than opening new surface; (C) investigations
that must run before they can even be scoped into a diff, and which block nothing else, so
they run alongside B/D; (D) turns this session's three new decisions into real code; (E) the
roadmap, pulled into this plan at the maintainer's request rather than deferred — independent of A-D,
schedulable in parallel with any of them.

```
Phase A — mechanical/trivial (parallel batch, dispatch immediately)
  ├─ TRK-267  tracker correction only (already merged via 927cfa0; fix the row, close #136)
  ├─ TRK-269  identity_conflict one-sided provenance (host_reconcile.py ~L585-589, add
  │           the dropped `source` key to the update-path new_value — ~4 lines)
  ├─ TRK-270  schema-drift gate: add `"compare_server_default": True` to both `opts` dicts
  │           in db/schema_check.py:149 and tests/test_migration_parity.py:156
  ├─ TRK-278  WindowsAgent dispatchable=False in supervisor.py AGENT_REGISTRY (1 line —
  │           still a Critical-Files touch: full pytest + routing test, per standing rule)
  └─ TRK-271  GitLab issue close-out only — confirmed by design (supervisor.py:46-58
              already documents drift/notification as schedule=None, hook-invoked;
              inventory_mr has skip_hook=True) — no code, just close #147 with the finding
        ↓
Phase B — finish the existing MCC ADR + one more scoped fix (parallel batch)
  ├─ Phase 2.3  record_rootcause_notes_bulk() — full blast-radius spec already written in
  │             §4.3 of the prior ADR; implement as specified there (dry_run default,
  │             100-item cap, per-item savepoint, same validation path as the existing
  │             single-note tool). Catalog update in mcp_auth.py in the same commit.
  ├─ Phase 3.1  TRK-247 runtime guard — detect an absent HTTP request context in the
  │             mutating tool bodies, log loudly, stamp `authored_by="direct:unattributed"`.
  │             TRK-247's actual decision text is (b)-only, detect-and-stamp, not "build
  │             both (a) and (b)" — corrected here from an earlier mischaracterization.
  │             2.3 is unconditional regardless (the prior ADR scoped it as a standalone
  │             mitigation, not contingent on the (a)/(b) choice); 3.1 implements exactly
  │             the (b) direction the maintainer decided, no more.
  ├─ TRK-268    fleet_health count_override fix — mirror the existing F-008 pattern already
  │             used in graph_maintenance.py; FleetHealthReporter.collect() needs the same
  │             count_override so its own health_snapshot stops being diffed as fleet drift.
  └─ TRK-272    MCP pagination/totals across get_drift_events, get_vulnerabilities,
               get_remediation_suggestions, get_inventory_gaps (mcp_server.py:526/570/613/625)
        ↓ (2.3 and 3.1 both touch mcp_server.py — declared overlap; rolling-integration
           folds whichever lands first, the second rebases onto it)
Phase C — live-data investigations (Fable-tier, read-only against deploy-host-01; blocks
          nothing in B/D, runs in parallel with them)
  ├─ TRK-273  netdiscovery null hostname/mac/mac_vendor — extraction logic exists
  │           (netdiscovery.py:900-946) but needs a live nmap-flag/DNS-reachability check
  │           before concluding whether it's a config gap or a real data gap
  └─ TRK-274  vsphere/host_reconcile/netdiscovery byte-identical counts across ~90 runs —
              no caching/dedup short-circuit found by source grep; needs a live query
              against collection_runs/the actual source APIs to find the real cause
        ↓ (either may terminate at "diagnosed, here's the real fix" rather than ship a diff
           — that is a valid deliverable, not a failure to scope)
Phase D — this session's newly-unblocked builds (medium, parallel)
  ├─ D.1  TRK-275  EOL: (a) doc/consumer fix — rename fields or document the real 0-100
  │                scale so `pci_risk_score` and Rapid7's `risk_score` can't be conflated;
  │                (b) normalize registry keys from vSphere VM name to canonical host
  │                identity — reuse host_reconcile's existing identity-resolution logic
  │                rather than inventing a second resolver. (b) is the larger sub-task;
  │                scope it as its own worktree task, separate from (a)'s docs fix.
  ├─ D.2  TRK-276  instinct-store sync: infra-brain DB authoritative, import infra-ops's
  │                YAML ledger into it. **Cross-repo dependency — confirmed reachable, not
  │                blocked:** the ledger is on-disk at
  │                `/home/youruser/git/infra-ops/knowledge/instincts/` (verified present
  │                during this session), readable directly, no separate access grant needed.
  │                Before building either a one-time import or a scheduled sync job, re-run
  │                `get_instincts(min_confidence=0.0)` against a live DB to confirm whether
  │                the store is really empty (GitLab #154 flagged this as
  │                observed-but-unconfirmed) — that result changes whether this is a sync
  │                job or a one-time backfill.
  └─ D.3  TRK-277  NotificationAgent: replace the hard early `return` at notification.py:166-169
                   with a path that still records an internal ack/audit row when Jira is
                   unconfigured, instead of silently skipping the entire sweep including
                   compliance-ticketing bookkeeping. Check `notify_incidents()`/
                   `notify_ops_alerts()`'s separate `ops_webhook_url` gate for the same
                   pattern while in this file (flagged, not fully traced, by the issue itself).
        ↓
Phase E — roadmap (pulled into this plan)
  ├─ E1  roadmap-agents (#94-103, 10 issues) — each issue body already contains its own
  │      scoped design (data model, graph edges, read-only notes — e.g. #94's PKI/CA
  │      monitoring sketch). Sequence by each issue's own stated ranking (#94 explicitly
  │      "Ranked #1 of 13"); left as-is per the maintainer's call, not re-ranked by effort here.
  │      Each: /agent-register → test (success/empty/exception) → lc-safety-reviewer +
  │      lc-agent-completeness → supervisor.py/scheduler.py wiring (Critical Files: full
  │      pytest + routing test per registration).
  │      #104 (app-layer topology mapping) and #105 (cost/billing) are scoping-only;
  │      #105 is explicitly blocked on a cloud-domain enablement decision — carried to the
  │      external-blockers list, not scheduled.
  ├─ E2  roadmap-automation (#108) — compliance rule-gap proposals → real GitLab MRs
  │      against compliance.yml. Extends the existing write-gated MR-drafting path (same
  │      shape as RemediationAgent's config-drift proposals). Sonnet-tier + lc-safety-reviewer
  │      (new external-write path).
  ├─ E3  roadmap-integrations (#111 chatops bot, #112 webhook-out, #114 GraphQL API,
  │      #118 policy-as-code engine) — 4 standalone subsystems, each its own build.
  │      #115 (Terraform/Pulumi hook) and #117 (multi-tenancy) are explicitly flagged
  │      scoping-only / "do not build without a real need" in their own issue titles —
  │      carried forward as flagged, not scheduled.
  └─ E4  unplanned batch: #129 (dormant collectors — cross-check against the still-
        undiagnosed k8s/net/cloud 13-day staleness gap before building, may be the same
        root cause), #130 (Ivanti licensing research, no code), #131 (forecast/lead-time
        tool, standalone new feature)
```

---

## 4. Review-tier summary (Critical-Files exposure)

| Phase item | Critical path touched | Required rigor |
|---|---|---|
| A: TRK-278 | `supervisor.py` (AGENT_REGISTRY) | Full pytest + routing test, even for a 1-line change |
| B: 2.3, 3.1 | none in the Critical-Files table, but `mcp_auth.py`'s catalog is enforcement-load-bearing (TRK-231 lesson) | Same-commit catalog update; existing parity test must pass |
| B: 2.3 | mutation tool, no schema change | `lc-safety-reviewer` (audit coverage, attribution, size caps) |
| D.1(b) | identity-resolution logic shared with `host_reconcile.py` | Regression tests on both call sites; no schema change expected but verify |
| D.2 | none in this repo directly, but a new cross-repo read path | Design the import boundary carefully — read-only against infra-ops's YAML, never write back |
| E1 (every roadmap agent) | `supervisor.py` + `scheduler.py` per registration | `lc-safety-reviewer` + `lc-agent-completeness` + routing tests, every time |
| E2 | new external-write path (GitLab MRs against compliance.yml) | `lc-safety-reviewer` |
| Everything else in A/B/C/D not listed above | none | Standard Sonnet-tier scoped implementation + tests |

---

## 5. Dependency graph / what starts when

```
NOW (parallel):   A (all 5 items)     C (TRK-273, TRK-274 — investigation only)
THEN:             B (2.3, 3.1, TRK-268, TRK-272) — 2.3/3.1 share mcp_server.py, declared
                  overlap, rolling-integration handles it
PARALLEL W/ B:    D.1, D.2, D.3 — all three newly unblocked, independent of each other
                  and of B
PARALLEL W/ ALL:  E1-E4 — independent of A-D; capacity-permitting, not gated on anything
                  above finishing first
CARRY FORWARD,    OPS_WEBHOOK_URL (TRK-135), Bedrock hold, TRK-099, TRK-249 (the maintainer checks
DO NOT SCHEDULE:  token), TRK-259 (watch), fix/6.1-mig-b6-flip (soaking), #105 (blocked
                  on cloud-domain decision), #115/#117 (flagged do-not-build)
```

Sequencing notes:
- All code-writing dispatches: `isolation:"worktree"` (mechanically enforced); commit locally
  early and often; push/MR only batched and only with the maintainer's confirmation (standing merge
  policy — never autonomous).
- Phase C items may terminate at "diagnosed, here's the real fix" — that outcome moves the
  item into a future phase's scope, it is not a failure to complete C.
- D.2's cross-repo read (infra-ops's YAML ledger, confirmed on-disk and reachable — see §3)
  still needs the `get_instincts` emptiness question resolved live before committing to a
  sync-job vs. one-time-backfill shape, but is not blocked on access.
- E1's roadmap-agents ranking is taken as-is from each issue's own stated priority, per
  the maintainer's explicit call not to re-rank by effort.

## 6. Decision

Adopt the phase ordering above (A → B/C in parallel → D in parallel with B/C → E in parallel
with everything). Dispatch Phase A and Phase C immediately in one parallel batch — A is
zero-risk and C blocks nothing. Phase B and D can begin as soon as worktree capacity allows
without waiting for A/C to finish, since none of them share files with A, and only 2.3/3.1
within B share a file with each other. Phase E dispatches independently on its own schedule,
capacity permitting, since nothing in A-D gates it.
