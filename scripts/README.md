# scripts/

Every script here has its own detailed docstring explaining what it does and why —
this file is just an index so you know which ones matter for actually running the
project versus which are historical evidence of operational rigor you can skip.

## Bootstrap / seed (you'll actually run these)

- `seed_db.py` — first-run Postgres seed.
- `create_admin.py` — idempotent dashboard admin bootstrap; the fallback for an
  already-running stack where `ADMIN_PASSWORD` was never set.
- `gen_agents_md.py` — regenerates the root `AGENTS.md` domain-agent roster from the
  live `AgentSpec` registry. CI-verified to stay in sync; run this after adding or
  changing a domain agent.
- `mint_headless_runner_key.py` — mints a scoped MCP API key for a non-interactive
  caller (so its actions attribute to `mcp:headless-runner` in the audit log, rather
  than an unattributed direct write).
- `probe_environment.py` — checks real reachability of every *configured* integration
  (GitLab, Ansible inventory, the active LLM gateway, container registries) and files
  the gaps it finds.
- `reasoner_tier_dry_run.py` — offline preview of the three opt-in reasoner-tier LLM
  features (root-cause reasoning, compliance gap-finding, remediation drafting)
  without needing real LLM credentials provisioned yet.

## Deploy / CI helpers

- `ci/wait_for_postgres.py` — used by the CI/deploy pipeline to block until Postgres
  accepts connections.
- `contract/generate.py` — regenerates the OpenAPI/TypeScript contract snapshot
  (`contract/generated/`) checked by CI against API drift.
- `deploy/downgrade_guard.sh`, `deploy/rollback_stack.sh` — deploy-pipeline rollback
  helpers.
- `design_sync/check_no_external_origins.py` — CI guard that fails if any external
  CDN origin shows up in the served frontend assets (fonts/scripts must be vendored
  locally, not loaded from a CDN). The directory name predates the current dashboard
  stack; the check itself is current and wired into pre-commit + CI.

## One-off historical fixes (safe to ignore)

These were each a real, narrow data-repair for a specific bug that has since been
fixed at the source — kept as evidence of how issues were actually diagnosed and
resolved, not because you'll ever need to run them again:

- `backfill_vuln_queue_severity.py`
- `close_graph_maintenance_drift_backlog.py`
- `dedupe_iac_resources.py`
- `flag_stale_wrong_direction_proposals.py`
- `repair_poisoned_inventory_reconcile_events.py`

## Needs infrastructure not in this repo

These assume access to systems specific to the original private deployment (a sibling
private repo, an internal wiki, a real GitLab instance) and won't do anything useful
against a fresh clone:

- `seed_instincts_from_infra_ops.py` — one-time import from a sibling private repo's
  instinct ledger.
- `sync_fleet_wiki_knowledge.py` — one-off sync from a sibling private wiki repo.
- `seed_host_purpose_map.py` — parses real Ansible inventory files not present here.
- `snapshot_gitlab_issues.py` — regenerates a GitLab issue snapshot against a private
  GitLab instance and a findings tracker, neither of which are part of this copy.
- `mint_buzz_acp_key.py` — mints a scoped MCP key for "Buzz," the maintainer's
  separate personal multi-agent coordination system. Not part of infra-brain itself;
  irrelevant unless you also run that system.

## Maintainer's personal tooling

- `store_dashboard_admin.py` — stores/verifies the dashboard admin credential in the
  maintainer's personal 1Password vault. Nothing here is infra-brain-specific; it's
  the maintainer's own credential-management habit, included for completeness rather
  than as something you'd reuse.
