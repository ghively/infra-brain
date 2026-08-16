# Langfuse v3 Observability Stack

Self-hosted Langfuse v3 (LLM tracing/observability) for infra-brain. This is a
**separate compose project** from `docker/docker-compose.yml` — it is deployed,
upgraded, and backed up independently.

**CI never deploys this stack.** It is operator-only, manual deploy. There is
no pipeline job, no automated migration gate, no scheduled restart. If it's
down, nothing in infra-brain's core loop (collection, drift detection, the
dashboard) is affected — this is an optional observability add-on, not a
dependency.

---

## ⚠️ Before you deploy: sizing and disk

**Check disk capacity FIRST.** This host hit 100% disk on 2026-07-12. Do not
start this stack without headroom — ClickHouse and MinIO volumes grow
unbounded with trace volume, and Postgres/ClickHouse will not degrade
gracefully when the disk fills.

```bash
df -h /var/lib/docker    # or wherever the Docker data-root lives on this host
```

**Nominal resource footprint (all services, mem_limit/cpus sums):**

| Service | mem_limit | cpus |
|---|---|---|
| langfuse-web | 2 GiB | 2.0 |
| langfuse-worker | 2 GiB | 2.0 |
| langfuse-postgres | 2 GiB | 1.0 |
| langfuse-clickhouse | **8 GiB** | 4.0 |
| langfuse-redis | 512 MiB | 0.5 |
| langfuse-minio | 1 GiB | 1.0 |
| **Total** | **~25 GiB** | **~11 cpus** |

**ClickHouse has an 8 GiB floor** — its merge/compaction working set does not
shrink below this even at modest trace volume. Do not lower `mem_limit` on
`langfuse-clickhouse` below 8g; it will OOM-kill under normal ingestion, not
just under load spikes. If the host cannot spare ~25 GiB / 11 cpus, do not
deploy this stack on it — provision a dedicated host/VM instead of shrinking
these limits.

Every service in this stack has both `mem_limit` and `cpus` set, and json-file
logging capped at `10m` × `3` files — this stack must not repeat the main
compose file's earlier DEPLOY-1 gap (uncapped resource limits).

---

## Deploy

1. **Disk check** (above) — do this first, every time, not just on first
   deploy.
2. **Create the shared network** (idempotent; only needs doing once per host):
   ```bash
   docker network create infra-brain_observability || true
   ```
   This is the network the main app stack (`docker/docker-compose.yml`) can
   join to export traces to `langfuse-web:3000` without exposing Langfuse on
   the app's own `backend` network. It is `external: true` in this compose
   file and is not owned/deleted by either project's `down`.
3. **Provision keys** (see "Key provisioning" below) into
   `docker/langfuse/.env` — copy from `.env.example`, never commit the filled
   file.
4. **Bring the stack up:**
   ```bash
   docker compose -f docker/langfuse/docker-compose.yml --env-file docker/langfuse/.env up -d
   ```
5. **Verify:**
   ```bash
   docker compose -f docker/langfuse/docker-compose.yml ps
   curl -sf http://localhost:3000/api/public/health
   ```
6. Log into the web UI at `http://<host>:3000`, create/verify the org and
   project (or confirm the `LANGFUSE_INIT_*` bootstrap vars provisioned one),
   and generate API keys for the infra-brain app to use for trace export.

---

## Key provisioning

Generate each of these once per deployment and store in Bitwarden (per
infra-brain's secrets policy — never hard-code, never commit):

```bash
openssl rand -hex 32   # NEXTAUTH_SECRET
openssl rand -hex 32   # SALT
openssl rand -hex 32   # ENCRYPTION_KEY   (must be exactly 32 bytes / 64 hex chars)
openssl rand -hex 16   # LANGFUSE_POSTGRES_PASSWORD / CLICKHOUSE_PASSWORD / MINIO_ROOT_PASSWORD
```

`ENCRYPTION_KEY` and `SALT` are used to encrypt stored API keys and other
sensitive fields inside Langfuse's own Postgres — rotating them after initial
provisioning requires a Langfuse-documented re-encryption migration, so treat
them as fixed for the life of the deployment (back them up alongside the DB,
not just in Bitwarden).

After the web UI is up, generate a Langfuse **project API key pair**
(public/secret key) through the UI or `LANGFUSE_INIT_*` bootstrap vars, and
add those to infra-brain's own `.env` (`LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST=http://langfuse-web:3000`) so the app
can export traces — see the main repo's `.env.example` for the app-side vars
once Phase 4 wires tracing in.

---

## Upgrade

Langfuse v3 ships DB migrations that run automatically on `langfuse-web`
startup. Standard procedure:

1. **Back up first** (see below) — migrations are not always reversible.
2. Bump the pinned tags in `docker-compose.yml` (`langfuse/langfuse:3`,
   `langfuse/langfuse-worker:3` — re-pin to a specific patch tag rather than
   tracking the moving `3` major tag once you've validated a target version).
3. Pull and recreate:
   ```bash
   docker compose -f docker/langfuse/docker-compose.yml --env-file docker/langfuse/.env pull
   docker compose -f docker/langfuse/docker-compose.yml --env-file docker/langfuse/.env up -d
   ```
4. Watch `langfuse-web` logs for migration completion before assuming the
   stack is healthy:
   ```bash
   docker compose -f docker/langfuse/docker-compose.yml logs -f langfuse-web
   ```
5. Re-run the disk check — ClickHouse schema migrations can transiently
   double disk usage during a large re-partition.

---

## Backup

Three stateful stores to back up; none of infra-brain's existing backup
tooling covers this project (it is a separate compose project on purpose).

- **Postgres** (metadata: users, projects, API keys, prompts):
  ```bash
  docker exec infra-brain_langfuse-postgres_1 pg_dump -U langfuse langfuse | gzip > langfuse-pg-$(date +%F).sql.gz
  ```
- **ClickHouse** (trace/observation events — largest volume, lowest criticality;
  losing recent traces is not a compliance issue for infra-brain, but do not
  assume that generalizes to every deployment):
  ```bash
  docker exec infra-brain_langfuse-clickhouse_1 clickhouse-client --query \
    "BACKUP DATABASE default TO Disk('backups', 'ch-$(date +%F)')"
  ```
- **MinIO** (large trace payloads / media blobs): use `mc mirror` against the
  `langfuse` bucket to an off-host target, or snapshot the
  `langfuse_minio_data` volume directly.

Restore is upgrade-in-reverse: stand up the target version, restore Postgres
first (schema/version-sensitive), then ClickHouse, then MinIO, then bring
`langfuse-web`/`langfuse-worker` up last.

---

## Notes

- `TELEMETRY_ENABLED=false` on both `langfuse-web` and `langfuse-worker` — no
  product-analytics phone-home from this self-hosted stack, consistent with
  infra-brain's audit posture.
- This stack does not touch infra-brain's own Postgres/Redis — `langfuse-postgres`
  and `langfuse-redis` are dedicated instances on the `infra-brain_langfuse`
  network, isolated from the app stack's `backend` network. Only
  `langfuse-web` also joins the external `infra-brain_observability` network,
  which is the sole intended integration point with the app stack.
- All named volumes are prefixed `infra-brain-langfuse_*` to avoid collision
  with the main stack's volumes and to make `docker volume ls` legible.
