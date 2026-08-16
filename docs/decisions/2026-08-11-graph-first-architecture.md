# Graph-First Architecture — collapsing the two edge stores and making graph contribution pluggable

**Status:** PROPOSED — design only, nothing implemented. Written 2026-08-11 at the
maintainer's request after a vision/implementation gap review.

**Grounding evidence** (all measured against the live deployed database and current
`main`, not inferred): `resources` 3,151 rows · `snapshots` 91,888 · `drift_events` 953 ·
`resource_relationships` 2,726 · `graph_nodes` 7 · `graph_edges` 0 ·
`graph_maintenance.py` 6,367 lines · graph layer total 9,864 lines ·
4 of 52 agent files reference the graph at all · `RelationshipType` 16 entries ·
19 of 30 collector domains unconfigured.

---

## 0. The one-sentence model

The knowledge graph becomes the **product core**: one edge store (`graph_nodes` /
`graph_edges`), holding **entities and their relationships** with full bitemporal history,
populated by collectors that **declare what they contribute** the same way they already
declare their schedule — replacing a 6,367-line central deriver that must be hand-taught
about every source.

---

## 1. The stated vision

> Collectors, pluggable, able to be turned off and on, with easy expansion to add
> additional ones. Collect all the data they can from their sources and store it in
> Postgres. Then a knowledge graph is made from all that data across all the sources for
> relationships, dependencies. **That knowledge graph is the foundational core.** Other
> agents — remediation, vulnerabilities — operate on top of it, also pluggable. At its
> core this is an agentic knowledge-graph database for current and historical recordings
> across all services, domains, logs.

Two things follow from that framing and are treated as requirements below:
**(a)** the graph is the thing everything else reads from, not a downstream artifact;
**(b)** "current *and historical*" is a first-class requirement, not a nice-to-have.

---

## 2. Where we actually are

### 2.1 Collection is genuinely pluggable — this part matches

`etl/spec.py`'s `AgentSpec` is a real declarative registry. `graph.py` derives its sweep
nodes from `etl.spec.sweep_members()` rather than a hardcoded list. `/agent-register`
scaffolds agent + test + supervisor entry + schedule in one command. Unconfigured
collectors raise `CollectorSkipped` and self-skip cleanly. `collection_disabled_domains`
exists for runtime on/off. **No change proposed here.**

### 2.2 There are two edge stores, and the one in use is the weaker one

| | `resource_relationships` — 2,726 rows, ACTIVE | `graph_edges` — 0 rows, IDLE |
|---|---|---|
| Can connect | **only rows in `resources`** (FK-constrained) | any `graph_nodes`; `resource_id` is **nullable**, so a node may be a CVE, service, team, log stream |
| History | `status` flag; UNIQUE on `(from, to, type)` → **one row per relationship, forever** | `valid_from`/`valid_to`; UNIQUE only `WHERE valid_to IS NULL` → supersede-don't-overwrite |
| True-at vs known-at | not modelled | `valid_from` vs `recorded_at` |
| Provenance | `source` string | `source` + `method` + `evidence` + `authority` (`auto`\|`human`) |

`graph_edges` is a **strict superset**. Critically, `resource_relationships`
**structurally cannot satisfy requirement (b)** — its unique constraint permits exactly
one row per relationship triple, so a changed relationship overwrites its own history.
"This VM ran on that host from March to July" is not expressible.

Both exist through accretion, not design: the relational table came first and was
adequate for resource→resource; the node/edge store was added later for cross-source
identity resolution, confidence and human confirmation. Nobody collapsed them, so the
simple one kept receiving writes and the capable one sat idle.

### 2.3 The graph is anti-pluggable — this is the core problem

`AgentSpec` declares `domain`, `tier`, `schedule`, `max_staleness`, `skip_hook`,
`dispatchable`, `collect_timeout_seconds`. **Every field governs when and whether a
collector runs. None describes what it contributes to the graph.**

That knowledge lives instead in `agents/graph_maintenance.py` — 6,367 lines that import
`LinuxHost`, `LinuxPackage`, `CiPipelineRun` and other domain tables *directly* and
hand-derive each relationship type (`VULNERABLE_TO`, `DEPLOYS_TO`, `PATCHED_BY`,
`HAS_SOFTWARE`, `TAGGED_AS`, …) against a central 16-entry `RelationshipType` enum.

Net effect — adding a source is two very different jobs:

| Task | Effort today |
|---|---|
| Add a collector that gathers data | scaffolded, one command |
| Make that data appear in the graph | edit a 6,367-line central file that must learn the new schema |

This is the exact inversion of the vision: pluggable *collection* feeding a
**monolithic, hand-maintained** graph derivation.

### 2.4 The observable symptom

Of 2,726 edges, **1,904 (70%) are `HAS_DRIFT` + `ON_FIELD`** — the `drift_events` table
decomposed into edges. Most of the remainder (`HAS_ACCOUNT`, `EXPOSES_PORT`, `HAS_MOUNT`,
`HAS_CRON`, `HAS_FIREWALL_RULE`, `HAS_PENDING_UPDATE`) is **containment**: "this host has
this attribute," a graph-shaped restatement of rows already in that host's detail tables.

Only ~150 edges (`BELONGS_TO`, `DEFINED_IN`, `ANSIBLE_MANAGES`) are genuine relationships
between distinct entities — and all come from one collector.

That is what a central deriver produces when decomposing a table it already knows is
cheaper than learning a source it doesn't.

### 2.5 The graph is also starved of inputs

19 of 30 domains are unconfigured. Cross-source correlation — the entire point of
"a graph **across** all sources" — needs ≥2 sources naming the same real-world things.
There is currently one hostname-bearing source. **The identity-resolution machinery is
built and tested; it has nothing to chew on.** This is configuration, not architecture,
but it bounds the value of everything below.

---

## 3. Target architecture

```
   collectors  ──declare──►  AgentSpec (nodes, edges, identity keys)
        │                              │
     raw facts                    generic graph engine
        │                              │
        ▼                              ▼
  domain tables  ◄──projection──   graph_nodes / graph_edges   ◄── agents read here
  (linux_ports,                    (ONE store, bitemporal)          (remediation,
   snapshots, …)                                                     rootcause, …)
```

Three changes:

1. **One edge store.** `graph_edges` only. `resource_relationships` is migrated and
   retired. A resource-backed node is simply a node with `resource_id` set.
2. **Graph contribution moves into the plugin contract.** Collectors declare nodes and
   edges; a generic engine materialises them.
3. **The monolith becomes an engine.** `graph_maintenance` stops importing domain tables
   and instead iterates registered contributions. Its genuinely cross-cutting work —
   confidence decay, contradiction reconciliation, stale-edge pruning, identity
   resolution — stays; that work is domain-agnostic and belongs there.

### 3.1 The modelling rule (the decision that keeps the graph clean)

**The graph holds entities and the relationships between them. Facts *about* an entity
stay as attributes or time-series attached to it, not as edges.**

| Belongs in the graph | Stays a fact/attribute |
|---|---|
| host ─`SAME_AS`→ r7 asset | listening ports |
| host ─`VULNERABLE_TO`→ CVE | installed packages |
| service ─`DEPENDS_ON`→ database | cron entries |
| deployment ─`DEPLOYS_TO`→ host | mounts, firewall rules |
| repo ─`DEFINES`→ service | drift events *(see §6)* |

Applied to today's data this removes roughly **2,500 of 2,726 edges** as
containment/decomposition, leaving relationships — which is the intent, not a regression.
Those facts remain fully queryable in their detail tables and reachable via their node.

Test for whether something is an edge: *does it connect two things that can each exist,
and be referred to, independently?* A CVE can. A cron entry cannot.

---

## 4. The plugin contract

```python
AgentSpec(
    domain="rapid7",
    tier=Tier.COLLECTOR,
    schedule="0 4 * * *",
    max_staleness=timedelta(hours=30),

    # NEW — what this source contributes to the graph
    emits_nodes=[
        NodeSpec(type="Asset", source_table=R7Asset,
                 natural_key="hostname", name="hostname",
                 resource_backed=True,
                 attributes=["os", "last_scan_at"]),
        NodeSpec(type="CVE", source_table=R7VulnCve,
                 natural_key="cve_id", name="cve_id",
                 resource_backed=False),          # not an inventory item
    ],
    emits_edges=[
        EdgeSpec(VULNERABLE_TO, frm="Asset", to="CVE",
                 via=R7Vulnerability, confidence=0.99, method="declared"),
    ],

    # NEW — how this source names real-world things, for cross-source identity
    identity_keys=["hostname", "mac_address", "serial"],
)
```

`identity_keys` is what makes correlation declarative. The resolver already knows *how* to
decide that two nodes are the same machine (`graph_phase3`, `SAME_AS`/`NOT_SAME_AS`, the
authority model from `2026-08-10-graph-edge-authority-spec.md`); what it lacks is a
declarative statement of what each source calls things. Today that is implicit in
`host_reconcile`'s hardcoded per-source leg handling.

**Consequence:** adding Rapid7 becomes *write the Rapid7 collector*. The graph gains it
with no central edit. That is the property the vision asks for and the current design
denies.

---

## 5. Migration path

Incremental and reversible. The engine runs **alongside** the existing deriver; nothing is
switched off until its replacement is proven.

| Phase | Work | Reversible? |
|---|---|---|
| **P0** | Add `emits_nodes` / `emits_edges` / `identity_keys` to `AgentSpec`, all optional and defaulting empty. Build the generic engine. Zero behaviour change — no collector declares anything yet. | trivially |
| **P1** | Migrate **one** relationship type (`DEFINED_IN`, 68 rows, single collector) to the declarative path. Run both writers; assert the engine reproduces the deriver's output exactly. | yes — delete the declaration |
| **P2** | Migrate the remaining genuine relationships (`BELONGS_TO`, `ANSIBLE_MANAGES`, `VULNERABLE_TO`, `DEPLOYS_TO`, `PATCHED_BY`, `HAS_SOFTWARE`, `TAGGED_AS`) one at a time, each with the same equivalence check. Delete each from the monolith only after its replacement matches. | per-type |
| **P3** | Backfill `resource_relationships` → `graph_edges` (2,726 rows; each becomes a node pair + one edge with `valid_from = created_at`). Containment edges per §3.1 are **not** migrated — they are dropped, with their underlying detail rows untouched. | via migration downgrade |
| **P4** | Point readers at `graph_edges`. Retire `resource_relationships` (drop last, after a soak). | table kept until P5 |
| **P5** | Drop `resource_relationships`. Delete the dead derivation code from `graph_maintenance`. | no — do last |

**Gate between every phase:** the three hard MR gates plus a live equivalence check on the
deployed database. Do not batch phases.

### 5.1 What changes per collector

For most collectors, nothing — they keep collecting into their own tables. Only those that
should contribute entities/relationships add a declaration. Expected initial set: `linux`,
`iac`, `cicd`, `vuln`/`vuln_cve`, `octopus`, `host_reconcile`. That is roughly **6 of 30**,
not a fleet-wide rewrite.

---

## 6. Open questions — decide before P2

1. **Drift.** Currently 70% of edges. Under §3.1 a drift event is a *fact about* a node,
   not an edge. Proposal: drift stays in `drift_events`, reachable via its resource's node,
   and `HAS_DRIFT`/`ON_FIELD` are not migrated. **The maintainer has separately flagged that drift
   semantics themselves are unclear ("I don't understand what drifted or why they are
   flagged as drift") — that is tracked separately and should be resolved before any
   drift-shaped graph decision is finalised.**
2. **Logs.** Different shape: high-volume, time-series, mostly not entities. Proposal: logs
   live in their own store; the graph holds *what they are about* (`Service ─EMITS→
   LogStream`), so a log query can start from a graph traversal without log lines becoming
   nodes. Not designed here; flagged so P0's contract does not preclude it.
3. **Query performance.** `resource_relationships` is faster for "everything attached to
   host X" (direct FK join). Mitigation: a materialised view or covering index over active
   edges. Measure at P3; do not pre-optimise.
4. **Do agents get rewritten to read the graph?** Not in this plan. Agents keep reading
   domain tables for facts; the graph is for relationships and traversal. Rewriting
   remediation/rootcause to be graph-first is a separate decision once the graph is
   populated enough to be worth reading.

---

## 7. What explicitly does NOT change

The read-only safety model (all three layers), the callback chain, `CollectorSkipped`
semantics and the `CollectOutcome` status contract, `ReconcileScope`, the scheduler,
`AgentSpec`'s existing fields, the bitemporal schema, the confidence-honesty rule,
decay's exemption for structural and human-confirmed edges, and the edge authority model
(`2026-08-10-graph-edge-authority-spec.md`) — that spec is a **dependency** of this one and
is already implemented.

---

## 8. Honest cost and sequencing

This is a meaningful project, not a weekend and not a rewrite. P0 is the only phase with
no user-visible payoff and it is the one that must be right — everything after it is
mechanical migration behind an equivalence check.

**Sequencing note that matters more than the code:** §2.5 means the graph's *value* is
currently gated on inputs, not architecture. Configuring a second real source (Rapid7 is
the natural candidate — vSphere is deliberately unused in this environment) would exercise
the cross-source identity machinery that is already built, and would validate the
modelling rule in §3.1 against real multi-source data **before** committing to it in a
plugin contract.

**Recommendation:** land a second source first, then P0. Designing the contract against one
source risks encoding that source's assumptions into the thing meant to abstract over all
of them.

> **Provenance note (restored 2026-08-13).** Everything above this line is the
> ORIGINAL PROPOSED document as written on 2026-08-11. It lived only on the
> unmerged branch `docs/graph-first-architecture` (commit `a7a8387`) — the
> addenda below were merged into `main` without it, so for two days this file
> on `main` consisted of addenda referring to sections (§3.1, §4, the migration
> path) that were not present. Restored verbatim; the addenda below record what
> the implementation actually did, and where it diverged from this sketch they
> are the authority.

---

## Addendum 2026-08-13 — TRK-359 closed: the junction grammar

The two P5 accepted losses are restored as declarations. The contract grew ONE
construct — `NodeSpec.row_gathers: tuple[RowGather, ...]`, legal only on a
`from_rows` (junction) node — and the edges needed no new vocabulary at all
(`from_key_multi` + `EdgeDirection.INVERSE`, both pre-existing).

**The insight that unblocked it:** the old refusal ("a from_rows node may not
gather") was about GROUPING, not about carrying lists. A per-parent `gathers`
list on a node whose identity recurs under many parents means whichever parent
was materialised last; a `RowGather` is grouped by the junction VALUE, so a
recurring value accumulates the deterministic sorted UNION. The refusal stands;
the grammar routes around it rather than relaxing it.

Two shapes, one per restored loss:

- **Deeper descent** (`path` non-empty): hops continue below the junction table
  by the same single-column-PK walk `ChildSpec` uses. `MEMBER_OF` (iac):
  `AnsibleInventoryGroup` from `ansible_inventory_groups` rows, members from
  `ansible_inventory_hosts`, stored `LinuxHost → group` at 0.900 via the `host`
  fold (the deriver's bare `.lower()` claimed 1.0 — folds more, claims less).
- **Anchor gather** (`path=()`): the gathered value is a field of the junction
  row's own anchoring `resources` row. `RUNS_EOL` (eol, the SEVENTH emitting
  domain): `EolProduct` from `eol_registry` rows, hosts gathered off the rows'
  anchors, stored `LinuxHost → product`. Registry rows anchored on minted
  `eol/product` resources produce no node — the deriver's self-loop guard,
  stated structurally.

The engine stayed provably domain-ignorant: both AST guard tests passed
**untouched** through this extension too. Equivalence was proven against the P5
audit's reconstruction queries (the derivers being long dead), computed
independently in `tests/agents/test_iac_member_of_graph.py` /
`test_eol_runs_eol_graph.py`; the grammar's accept/reject paths are pinned over
fictional domains in `tests/test_graph_engine_junction.py`.

`PART_OF` remains deliberately undeclared — no longer for a grammar reason, but
because it names the identical fact `MEMBER_OF` now carries, and one fact under
two labels is a contradiction waiting to drift.

**Boundaries that REMAIN refused after the junction grammar** (each pinned by a
test):

- **Many-to-many where NEITHER endpoint's collector owns the join rows** — a
  join functional from neither side still has no direction to declare it from
  (`test_a_many_to_many_relationship_is_still_not_expressible`, unchanged). A
  junction node only helps when ONE collector owns the junction table and can
  anchor a `from_rows` descent into it.
- **The one-target ambiguity index** — a fanned member two targets claim is
  still refused, never guessed
  (`test_a_fanned_member_with_two_claimant_targets_is_still_refused`).
- **Business-key hops** — `ChildSpec`/`RowGather` walk PK joins only, unchanged.
- **JSON-list child columns** — still refused, never flattened
  (`test_a_list_valued_gather_column_is_refused_not_flattened`).
- **Per-parent child entities / composite identities** — a junction node is
  still identified by ONE child value; per-parent entities remain a further
  contract decision.
- **Heterogeneous anchor populations** — a junction node reads ONE
  `(domain, resource_type)` anchor population. `eol_registry` rows anchored on
  vsphere/windows/r7 hosts (retired domains, zero live rows) or on minted
  product resources contribute nothing; widening is one NodeSpec per anchor
  type the day a second host domain is live.

Still open from the original plan after this: edge retirement ("saw the full
population" signal), `identity_keys` consumption by the resolver, and promoting
`agents/drift.py`'s module-local sets into per-collector declarations —
TRK-359's grammar item drops off that list.

---

## Addendum 2026-08-12 (evening) — P3, P4 and P5 complete: there is one store

The plan below this line is **finished**. `resource_relationships` no longer exists;
`graph_nodes`/`graph_edges` is the only edge store. Execution record:

- **P3** (TRK-357): backfill migration `8965b6329b94` — 101 historical edges recovered
  live, containment refused by the enumerated `CONTAINMENT_TYPES` allow-list.
- **P4** (TRK-364, MR !40): readers re-pointed; live-verified on production.
- **P5** (TRK-358..363, branch `feat/p5-graph`): six parallel worktree agents off
  `316476c`, folded T4 → T5-A → T5-B → T2 → T3-drop-last:
  - **Resolver switch** (TRK-361): `graph_phase3.resolve_entities` is the sole identity
    writer; `host_reconcile`'s IS_SAME_AS emitters deleted after all three coverage gaps
    were closed red-first (`HOST_NODE_TYPES` +LinuxHost +NetDiscoveredHost,
    `NodeSpec.is_host_identity` + guard test, shared-IP review-band floor with the
    routability/3-claimant guards ported from the dying emitter).
  - **Writer removal** (TRK-358 whole-method, TRK-360 surgical): every
    `emit_edge`/`emit_edges_batch` call site deleted; `graph_maintenance.py`
    6,355 → 986 lines; each deletion carries an epitaph naming its reinstating
    declaration path. Measured: 2,504 → 0 legacy rows and 406 → 0 minted convergence
    Resources per maintenance pass, ~2× faster.
  - **Consumer surface** (TRK-362): legacy routes and `get_neighborhood` gone; chat and
    the dashboard read `graph_kg` (BFS that runs identically on SQLite and PostgreSQL).
  - **The drop** (TRK-363): migration `95d988b2bc3c` after a row-by-row derivability
    audit; `db/relationships.py` is vocabulary + the four canonical absence sets
    (`DEFERRED` / `MIGRATED_TO_GRAPH_EDGES` / `RETIRED_CONTAINMENT_DERIVATIONS` /
    `RETIRED_WITH_LEGACY_STORE`).
  - **Two accepted losses, on record** (TRK-359 — **closed 2026-08-13**, see the
    addendum above): `MEMBER_OF` and `RUNS_EOL` were derived by nothing until their
    declaration paths landed; the facts stayed fully queryable in
    `ansible_inventory_*` / `eol_registry` throughout.

Still open from the original plan after P5: edge retirement ("saw the full population"
signal), `identity_keys` consumption by the resolver, TRK-359's declaration grammar
(**closed 2026-08-13** — the junction addendum above), and promoting
`agents/drift.py`'s module-local sets into per-collector declarations.

---

## Addendum 2026-08-12 — implementation status after the first two days

P0–P2 are **merged and deployed**; the contract survived contact with reality but not
unchanged. This section records what actually shipped, because the sketch in §4 is now
historical.

### What the contract became

| Piece | Shipped form | Why it differs from the sketch |
|---|---|---|
| Node source | `NodeSpec(domain, resource_type)` over the generic `resources` table | `source_table=<ORM class>` would have put a domain import back into the contract — the exact thing being removed |
| Edge types | validated free strings | the central `GraphEdgeType` enum is locked by a test asserting exactly five members, a guard whose purpose is blocking speculative central additions |
| Direction | `EdgeSpec.direction: {FORWARD, INVERSE}` — `from_node`/`to_node` describe the JOIN, direction decides how the edge is STORED | a one-to-many join is functional from its "many" side by construction; an inverse edge runs the identical join through the byte-for-byte untouched ambiguity index |
| Child tables | `ChildSpec(key, path, column)` — ordered `(table, fk)` descent resolved via shared SQLAlchemy `MetaData`, never model imports; `NodeSpec.gathers` (values ride onto the parent as a sorted list), `NodeSpec.from_rows` (values become shared, necessarily non-resource-backed nodes), `EdgeSpec.from_key_multi` (many-valued source key, each value still resolved key→one-target) | the join keys for `RUNS_IMAGE`/`ANSIBLE_MANAGES` live in child tables with no `resources` row and no metadata representation |

The engine remains provably domain-ignorant: two AST guard tests (no collector imports,
no domain/type-name branching) have passed **untouched** through every extension. That is
the property that keeps this from becoming the next 6,367-line file.

### Relationship migration scoreboard

| Relationship | Status | Note |
|---|---|---|
| `RUNS_ON` (service→host) | **declared** (P1) | first cross-collector declaration; hyphen/underscore join via `hostmatch.graph_match_key` |
| `BELONGS_TO` | **declared** (P2) | keyed on immutable `project_id` after a mutable-name bug was caught pre-ship |
| `DEFINED_IN` | **declared** | first INVERSE edge; stored as the exact mirror of BELONGS_TO |
| `RUNS_IMAGE` | **declared** | first child-table declaration; iac owns `ContainerImage` (the compose file naming an image is the source asserting it exists) |
| `ANSIBLE_MANAGES` | **hand-written, blocked on OWNERSHIP not reach** | the stored edge runs cicd's entity → linux's entity; iac owns only the join rows. Needs a decision: re-anchor the edge on the `AnsibleInventoryFile` iac owns (a different, arguably better edge — but one that fails the equivalence discipline), or widen the ownership rule with a junction-declaration form. Deferred deliberately; do not widen the rule to unblock one edge. |

Every migration used the two-commit equivalence discipline (declaration proven equal to
the live deriver first, deriver deleted second with the oracle frozen verbatim in a test
that keeps running). One deliberate divergence is pinned by a test: the old deriver kept
emitting for retired compose files; the declarative path excludes retired rows.

### Boundaries that remain, pinned executably

*(historical as of 2026-08-13 — the junction addendum at the top of this file
carries the current list; the "many-to-many" entry below has since been PARTLY
closed: a junction whose rows one collector owns is now declarable via
`NodeSpec.row_gathers`, while a join functional from neither side with no owned
junction table remains refused.)*

- **Many-to-many** — a join functional from neither side has no direction to declare it
  from; needs a junction declaration, refused loudly in both directions today.
- **Business-key hops** — `ChildSpec` walks PK joins only; `gitlab_projects.gitlab_project_id
  = iac_files.gitlab_project_id` is deliberately inexpressible (it is what makes the
  descent grammar safe to resolve generically).
- **JSON-list child columns** — refused rather than flattened (`ansible_playbook_plays.hosts`).

### Still open from the original plan

P3 (backfill `resource_relationships` → `graph_edges` + drop containment edges), P4/P5
(retire the old store), edge retirement (needs a "saw the full population" signal),
`identity_keys` consumption by the resolver, and promoting the two module-local sets that
landed in `agents/drift.py` (`_DURABLE_RESOURCE_TYPES`, `_EVENT_SHAPED_TYPES`) into
per-collector declarations.
