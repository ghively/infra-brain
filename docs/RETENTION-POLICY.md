# infra-brain Data Retention Policy (F-030)

Status: ACTIVE — enforced by the daily scheduler job `infra_brain_retention_prune`
(04:30 UTC, `src/infra_brain/retention.py`). Windows are configurable via the
`RETENTION_*` environment variables (`Settings` fields; see `.env.example`).

| Table | Window (default) | Cutoff column | Rule |
|---|---|---|---|
| `audit_log` | 400 days | `ts` | Hard age cutoff. PCI DSS 10.7 requires >= 1 year of audit-trail history (3 months immediately available); 400 days provides margin. |
| `agent_action_log` | 400 days | `ts` | Same PCI rationale as `audit_log`. |
| `snapshots` | 90 days | `collected_at` | Point-in-time collection payloads; drift derived from them is retained separately. |
| `resource_configs` | 90 days | `collected_at` | Same class of data as snapshots. |
| `drift_events` | 180 days | `detected_at` | RESOLVED rows only (`status != 'open'`). Open drift is never age-pruned. |
| `collection_runs` | 180 days | `started_at` | Child `snapshots`/`resource_configs` of an expired run are deleted first; surviving `drift_events` are detached (`collection_run_id -> NULL`). |
| LangGraph `checkpoints` (+ `checkpoint_blobs`, `checkpoint_writes`) | `RETENTION_CHECKPOINTS_DAYS` | `checkpoint->>'ts'` (JSONB) | Prunes stale threads via `retention.prune_checkpoints` (TRK-069). A `thread_id` is deleted only when its newest checkpoint is older than the window AND it is not referenced by any `ProposedAction` with `status IN ('pending','approved')`. Skips cleanly when the checkpoint tables are absent (MemorySaver-only / fresh env). |

Out of scope (already bounded elsewhere): `vsphere_*_metrics`
(`VSPHERE_METRICS_RETENTION_DAYS`, pruned by the vsphere agent), knowledge-graph
edges (`graph_maintenance._prune_stale_edges`), iac child tables (replace-on-write).

## PCI notes
- Requirement 10.7: audit-trail history retained >= 1 year — satisfied by the
  400-day windows on `audit_log`/`agent_action_log`.
- The prune job logs a per-table deleted-row count at INFO on every run
  (`[retention] prune complete: {...}`), providing evidence the policy executes.
- Disabling: set `RETENTION_PRUNE_ENABLED=false` (e.g. legal hold). Re-enable
  promptly; growth is unbounded while disabled.

## Changing a window
Change the `RETENTION_*` value in the deployment `.env` (and Bitwarden if
secret-managed), redeploy, and record the change + reason in this file.

## Migration status

The three prune-path indexes declared in `src/infra_brain/db/models/core.py`
(`ix_snapshots_collected_at`, `ix_snapshots_run_id`,
`ix_resource_configs_collected_at`) **shipped** in migration
`alembic/versions/0026_reconcile_live_drift.py`. That migration creates each
index behind an inspector existence check (idempotent — a no-op on any DB that
already has them, e.g. a fresh CI DB built via `0001`'s `create_all`), and its
`downgrade()` drops all three. No further migration work is outstanding for
these indexes.

## Checkpoint pruning

LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`) are not ORM-mapped and are pruned by
`retention.prune_checkpoints` (TRK-069), which runs in the **same daily
retention job** (`prune_expired`) as the table windows above. The window is the
`retention_checkpoints_days` `Settings` field (`RETENTION_CHECKPOINTS_DAYS`).
Because these tables carry no `created_at` column, staleness is measured from
the checkpoint's own JSONB timestamp (`checkpoint->>'ts'`): a `thread_id` is
eligible only when its newest checkpoint predates the window. Eligible threads
are still protected from deletion if referenced by a `ProposedAction` with
`status IN ('pending', 'approved')` (guards live human-in-the-loop interrupt
state). When the checkpoint tables do not exist (MemorySaver-only or fresh
environments), the prune skips cleanly and returns 0.
