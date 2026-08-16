# Changelog

Notable changes to infra-brain, grouped by theme rather than by individual commit or
merge request (this is a curated summary, not a literal `git log`).

---

## Graph-first architecture (2026-08)

- Collapsed two parallel, drifting stores of "how resources relate to each other"
  (a hand-maintained deriver and an ad-hoc relationships table) into one declarative
  graph engine: domain agents declare their nodes/edges via `AgentSpec`, and
  `graph_engine.py` emits them consistently, with authority/provenance tracked per
  edge (declared vs. inferred, confidence-scored). See
  `docs/decisions/2026-08-11-graph-first-architecture.md` and
  `docs/decisions/2026-08-10-graph-edge-authority-spec.md`.
- Identity resolution hardened: cross-source host/asset matching now has an explicit
  authority model and fuzzy-match confidence bands, replacing several silent
  false-merge and false-split failure modes found in earlier passes.
- Hybrid RAG retrieval (dense + keyword, reciprocal rank fusion) added for the
  self-improvement/knowledge layer.

## Dashboard rebuild (2026-07)

- The dashboard was rebuilt from a bespoke server-rendered framework to a
  Vite + React SPA (`dashboard-app/`), served at `/dashboard2`. See
  [DR-6.1](docs/decisions/DR-6.1-dashboard-stack.md) for the full rationale and
  migration record. The legacy dashboard and its build pipeline were deleted once
  the new one reached parity.
- New pages: LLM observability, chat, onboarding/empty-states, a settings catalog,
  and a reorganized navigation shell.
- Fixed along the way: several counts/badges that silently diverged from the data
  they summarized, a knowledge-graph identity-merge bug, and a chat-history
  truncation bug that had been misreported as database corruption.

## Safety-chain and audit hardening (2026-07)

A five-domain audit pass (frontend, backend, database, agents/graph, docs) found and
fixed a cluster of issues, the most serious being:

- **Chat safety-chain bypass**: the chat agent's LLM invocation wasn't passing
  `callbacks=`, so the read-only/DLP/audit chain silently never fired on chat tool
  calls. Fixed by attaching callbacks at the graph-invocation config level (verified
  this also propagates to tool-node execution, which constructor-level attachment
  does not).
- **PAN masking**: Octopus Deploy API responses are now Luhn-validated and masked
  before reaching the ORM or dashboard.
- **Credential handling**: Windows collector credentials moved from subprocess argv
  (visible in process listings) to the subprocess environment.
- **Constant-time comparison** for the MCP bearer token check; server-side session
  revocation via a Redis JWT-ID store (logout now actually invalidates the token,
  not just the client-side cookie).
- **Schema-drift guard** upgraded from a hardcoded allowlist of previously-seen
  columns to a full `compare_metadata` comparison — any drift on any table now
  blocks startup, not just columns someone remembered to allowlist after a prior
  incident.

## Data/collection correctness (2026-07)

- Several long-tail collectors (Linux users/groups/crons/mounts/NICs, Windows
  certificates/firewall/shares/local-admins) were wired up after being present as
  ORM models but never actually populated.
- Fixed a `drift_count` bug that had been silently zero for every collection run
  (collectors weren't stamping `collection_run_id` on drift events).
- Fixed inverted `DEPLOYS_TO`/`DEPLOYED_TO` graph edge directions.
- Added retry-with-backoff (via `tenacity`) to the Rapid7/Octopus/GitLab HTTP
  helpers for transient failures and rate limits.

## Structural cleanup (2026-06 – 2026-07)

- `dashboard_api.py` (4,000+ lines) and `db/models.py` (1,700+ lines) — the two
  largest "god files" in the codebase — were split into focused, domain-scoped
  modules (`api/routers/*.py`, `db/models/*.py`), each re-exported from a shim for
  backwards compatibility.
- ~30 list endpoints were migrated onto one shared `{items, total, limit, offset}`
  pagination envelope, enforced by a contract test across the whole API surface.
- CI gained a real migration-parity check (applies every migration against a live
  ephemeral Postgres and diffs the result, rather than checking the final schema
  shape via `create_all()`).

## Initial build-out (2026-06)

- Core safety chain shipped: `ReadOnlyToolValidator` (hard-denies non-GET calls),
  `DLPCallbackHandler` (PAN/secret scanning with fail-closed mode), `AuditCallbackHandler`
  (append-only audit log), `ObservationCallbackHandler`.
- First wave of read-only collector tools and domain agents (GitLab, vSphere,
  Rapid7, Octopus Deploy, Ansible IaC, Jira/Confluence for the write-adjacent
  agents).
- The self-improvement loop: generated-script persistence, a reasoning-trace log
  per agent iteration, and a `learning.build_context()` step that feeds prior
  instincts/observations back into the next reasoning pass.
- LangSmith tracing scaffolding; the first version of the dashboard, README, and
  operator guide.
