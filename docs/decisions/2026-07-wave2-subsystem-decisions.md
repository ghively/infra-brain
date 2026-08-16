# Decision record — approved-but-unbuilt subsystems (F-040, ROADMAP 2.8)

Date: 2026-07-04
Status: ACCEPTED (committed via MR per the repo's MR-gated merge policy)
Finding: F-040 (docs/audit/FINDINGS.md) — two designs were approved before the
2026-07 audit; the audit verified neither subsystem's implementation status.
This record closes that gap with explicit, committed decisions.

---

## 1. Memory / retrieval layer — DECISION: KEEP-DEFERRED

- Spec: docs/superpowers/specs/2026-06-19-brain-memory-retrieval-design.md
- Verified state (re-checked at this MR's HEAD): NOT implemented.
  `grep -rn "memory_chunks|MemoryChunk" src/ alembic/` returns nothing — no
  model, no migration, no runtime reference. No runtime path depends on it
  (the F-040 acceptance condition holds by construction).
- Decision: KEEP-DEFERRED. Not DROP: the design remains sound and wanted.
  Not IMPLEMENT now: Wave 2's mandate is stopping silent data loss in the
  existing pipeline; a new retrieval subsystem belongs after the Wave 4
  LangGraph adoption (checkpointer + Store API are its natural substrate —
  see ARCHITECTURE-RECOMMENDATIONS.md §4 and ROADMAP 4.2).
- Revisit trigger: when ROADMAP item 4.2 is merged, open a scoped design
  review of the 2026-06-19 spec against the then-current LangGraph Store
  API before any implementation MR.
- Guard: if any future change introduces a runtime dependency on
  `memory_chunks` before the layer exists, that change is wrong — the table
  does not exist. (The audit's F-040 grep is the check.)

## 2. Design-sync roundtrip tooling — DECISION: KEEP (implemented-in-scripts)

- Spec: docs/superpowers/specs/2026-06-23-design-sync-roundtrip-design.md
- Verified state (re-checked at this MR's HEAD): IMPLEMENTED, but not under
  `src/`. The tooling lives at `scripts/design_sync/` (assemble, build,
  build_and_stage, build_pages, check_no_external_origins, manifest,
  render_verify, sync, transform, manifest.json) with a dedicated test
  suite at `tests/design_sync/` (12 modules), green in CI.
- Correction to the audit record: F-040(b) said "no implementation in
  src/" — literally true and materially incomplete. The subsystem is
  build-time tooling, deliberately outside the runtime package; `src/`
  placement was never the operative requirement.
- Decision: KEEP as-is at `scripts/design_sync/`. No migration into
  `src/infra_brain/` — it must not import into (or ship with) the runtime
  image.
- Follow-through: FINDINGS.md F-040 should gain a one-line pointer to this
  record during the next corpus-hygiene pass (RE-VERIFICATION §5 work) —
  wiki/audit entries are additive, so this record does NOT edit FINDINGS.md
  itself.

---

## Acceptance mapping (ROADMAP 2.8)

- "each subsystem has a committed decision" — this file, merged via MR.
- "no runtime path depends on the unbuilt memory layer" — verification
  command and empty result recorded in the MR description:
  `grep -rn "memory_chunks|MemoryChunk" src/ alembic/` → no output.
