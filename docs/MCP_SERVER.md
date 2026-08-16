# Infra Brain — MCP Server Reference

The MCP server exposes all of Infra Brain's collected infrastructure data and
management operations to Claude Code (and any other MCP client) over a
streamable-HTTP transport.

- **Source:** `src/infra_brain/mcp_server.py` (tools + auth middleware),
  `src/infra_brain/mcp_auth.py` (key CRUD + the canonical tool-name catalog)
- **Port:** `8002` (host-mapped from the `mcp` Docker Compose service)
- **Transport:** MCP streamable-HTTP (`/mcp`)
- **Auth:** Per-key scoped bearer tokens (`ibmcp_…`), stored sha256-hashed in the
  `mcp_api_keys` table. There is no global token.
- **Tools:** 96 registered — 77 read-only, 19 requiring write scope
- **Live endpoint:** `http://192.0.2.13:8002/mcp`

---

## Architecture

```
Claude Code (infra-ops)
  │
  │  streamable-HTTP  Authorization: Bearer ibmcp_<token>
  ▼
_ApiKeyAuthMiddleware (pure ASGI, wraps the whole app)
  │  resolves the key, then checks EVERY requested tool name
  │  against that key's allowed_tools BEFORE dispatch
  ▼
Infra Brain MCP server  :8002
  │                │
  │ ORM queries    │ POST /sweeps/{domain}
  ▼                ▼
PostgreSQL       app :8000
(infra_brain DB) (trigger_collection only)
```

The MCP server is **read-only on infrastructure** — it queries the PostgreSQL
database that the collector agents populate. It holds no Docker socket and no
credentials for any managed system. Every write it performs lands in Infra
Brain's own database:

- `proposed_actions.status` → `approved` / `rejected` (single and bulk)
- New `instincts` rows (`promote_instinct`)
- New / updated `eol_registry` rows (`add_eol_product`)
- Manually seeded `resources`, `drift_events`, `vuln_queue` rows (`seed_*`)
- `root_cause_notes` rows and `compliance_rule_gap` proposals — always stamped
  as manual/MCP-authored, never presented as agent LLM output
- Confirmed / retracted `SAME_AS` edges in `graph_edges`
- Batch status flips on `drift_events` / `compliance_violations`
- `agent_action_log` audit rows (including one per auth denial)

Claude Code (infra-ops) remains the actor that opens GitLab MRs and runs
Ansible playbooks. Infra Brain surfaces the data; infra-ops pulls the trigger.

---

## Authentication

Auth is **per-key scoped bearer tokens**, not a single shared secret. Each key is a
row in `mcp_api_keys` (`src/infra_brain/mcp_auth.py`, model in
`db/models/core.py`) with its own `allowed_tools` list. The raw token is generated
as `ibmcp_<43 urlsafe chars>` and is **shown exactly once** — only its sha256 hex
digest is stored, so a lost token is re-minted, never recovered. Revocation is soft
(`revoked_at`), which keeps the audit trail intact.

Every request must include:

```
Authorization: Bearer ibmcp_<token>
```

Enforcement lives in `_ApiKeyAuthMiddleware`, a pure-ASGI middleware wrapping the
whole app (`mcp_server.py`). It buffers the JSON-RPC body, extracts **every**
requested tool name from it — a single `tools/call` object *or* each entry of a
JSON-RPC batch array — and denies unless *all* of them are in the key's
`allowed_tools`. It **fails closed**: a body it cannot confidently classify is
denied rather than waved through. Deny reasons are stable codes, returned in the
JSON error body as `reason` and also written to `agent_action_log.error`:

| Reason code | Status | Meaning |
|---|---|---|
| `key_invalid_or_expired` | `401` | No active `mcp_api_keys` row matches this token |
| `key_lacks_scope` | `403` | Valid key, but a requested tool is not in its `allowed_tools` |
| `unparseable_request` | `403` | Body shape could not be classified for scope checking |

A request with no `Authorization` header at all is rejected `401` before the body is
buffered, and is deliberately *not* audited (an unauthenticated prober must not be
able to drive unbounded DB writes). `/metrics` and `/healthz` are exempt from auth.
`INFRA_BRAIN_DEV=1` disables auth entirely for local dev — `assert_dev_not_in_hardened_env()`
makes that a hard startup failure when `ENVIRONMENT` is a hardened deployment.

### Minting a key

Three paths, all producing the same kind of key.

**1. Dashboard (normal path).** Log in as an admin and use **MCP Keys**
(`/dashboard2/mcpkeys`). The create form's tool multi-select is built from
`mcp_auth.TOOL_GROUPS` (19 domain groups) and offers "select all read-only" /
"select all mutation" toggles, so you can scope a key by domain rather than by
hand-listing names. Copy the token from the one-time reveal.

**2. Bootstrap CLI (first key on a fresh stack).** Mints a single full-access key
named `bootstrap` scoped to all 96 tools:

```bash
docker compose -f docker/docker-compose.yml exec mcp python -m infra_brain.mcp_auth --bootstrap
```

It prints `bootstrap key id=<uuid>` and `RAW TOKEN (shown once, not stored): ibmcp_…`.
Anything other than `--bootstrap` just prints usage. Use this to get in, then mint
properly-scoped keys from the dashboard and revoke the bootstrap key.

**3. HTTP API (scriptable path).** The same routes the dashboard uses
(`api/routers/mcp_keys.py`) — dashboard *session* auth, admin-only for mutations,
so log in first and keep the cookie. Against the live stack (`app` is host port
`8001`):

```bash
# 1. log in as a dashboard admin, storing the session cookie
curl -sS -c /tmp/ib.jar -X POST http://192.0.2.13:8001/api/dashboard/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}'

# 2. inspect the catalog you can scope against (readonly / mutation / groups)
curl -sS -b /tmp/ib.jar http://192.0.2.13:8001/api/dashboard/mcp-keys/tools

# 3. mint a read-only key scoped to all 77 read-only tools
curl -sS -b /tmp/ib.jar http://192.0.2.13:8001/api/dashboard/mcp-keys/tools \
  | jq '{name: "infra-ops-readonly", allowed_tools: .readonly}' \
  | curl -sS -b /tmp/ib.jar -X POST http://192.0.2.13:8001/api/dashboard/mcp-keys \
      -H 'Content-Type: application/json' --data-binary @-
```

The create response is the only place the raw token appears:
`{"id": …, "name": …, "token": "ibmcp_…", "allowed_tools": [...]}`.

To scope a key to one group instead, swap the `jq` filter — e.g.
`'{name: "vsphere-readonly", allowed_tools: .groups["vSphere"]}'`. An unknown tool
name is a `422`; the catalog is validated against `mcp_auth.ALL_TOOL_NAMES`.

Amend a live key's scope in place (never re-issues the token, `409` on a revoked
key), or revoke it:

```bash
curl -sS -b /tmp/ib.jar -X PATCH http://192.0.2.13:8001/api/dashboard/mcp-keys/<key_id> \
  -H 'Content-Type: application/json' -d '{"allowed_tools":["query_resources","get_drift_events"]}'

curl -sS -b /tmp/ib.jar -X POST http://192.0.2.13:8001/api/dashboard/mcp-keys/<key_id>/revoke
```

### Attribution

Tools that record who did something (`approve_proposal`, `record_rootcause_note`,
`record_compliance_gap`, …) derive the author **server-side from the authenticated
key** as `mcp:<key name>`. A caller-supplied label is only ever appended as a quoted
claim — `mcp:<key name> (says: <label>)` — and can never replace the identity part.
Name your keys accordingly; the key name is what shows up in the audit trail.

---

## Connecting from Claude Code

The `infra-brain` MCP server is wired in `C:\path\to\infra-ops\.claude\settings.json`:

```json
"infra-brain": {
  "type": "streamable-http",
  "url": "http://192.0.2.13:8002/mcp",
  "headers": {
    "Authorization": "Bearer ${INFRA_BRAIN_MCP_KEY}"
  }
}
```

`INFRA_BRAIN_MCP_KEY` is the raw `ibmcp_…` token from the minting step above, set in
`C:\path\to\infra-ops\.env` (the variable name is the client's choice — the server
only reads the `Authorization` header). A pre-2026-07-23 hex `INFRA_BRAIN_MCP_TOKEN`
value will 401 forever: it is not an `ibmcp_` token and hashes to no row in
`mcp_api_keys`. The tools appear as `mcp__infra-brain__<tool_name>` after restarting
the Claude Code session, limited to the ones that key is scoped to.

The client must also accept **both** `application/json` and `text/event-stream` —
that requirement comes from the MCP streamable-HTTP transport itself, not from
infra-brain (see Troubleshooting).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `INFRA_BRAIN_MCP_ENABLE_MUTATIONS` | `false` | Process-wide gate on mutating tools. **Layered on top of key scope — both are required.** A key scoped to `approve_proposal` still gets `{"error": "mutating MCP tools are disabled; …"}` (HTTP 200) while this is off. |
| `INFRA_BRAIN_DEV` | `false` | `1` runs the MCP server with **no authentication** (local dev only). Refuses to boot when `ENVIRONMENT` is a hardened deployment. |
| `ENVIRONMENT` | `development` | `deployed` (legacy alias `production`) marks a hardened stack and makes `INFRA_BRAIN_DEV` a hard startup failure. |
| `INFRA_BRAIN_MR_ENABLED` | `false` | Enable GitLab MR creation in `remediation` + `inventory_reconcile` agents. Leave `false` — Claude Code / infra-ops opens MRs. |
| `INFRA_BRAIN_APP_URL` | `http://app:8000` | Internal URL used by `trigger_collection` to POST `/sweeps/{domain}`. Override if MCP runs outside the compose network. |
| `MCP_TOOL_TIMEOUT_SECONDS` | `60` | Wall-clock ceiling for LLM-backed MCP tools (today: `query_nl`). `0` disables. |
| `MCP_PORT` | `8002` | Port the MCP server listens on. |

There is no `INFRA_BRAIN_MCP_TOKEN` and no `INFRA_BRAIN_ENV_PATH` — the first was
replaced by scoped keys, the second existed only for the removed `set_secret` /
`update_config` tools.

---

## Tool Catalog

96 tools are registered on the server: **77 read-only**
(`mcp_auth.READONLY_TOOL_NAMES`) and **19 requiring write scope**
(`mcp_auth.MUTATION_TOOL_NAMES`). `mcp_auth.py` is the single source of truth —
`tests/test_mcp_auth_helpers.py::test_catalog_matches_registered_mcp_tools` fails
the build if it ever drifts from the tools actually registered in `mcp_server.py`,
and a tool missing from the catalog is unreachable by *every* key including
bootstrap.

The sections below document the core tools in detail. For the full list as the
server sees it right now, use `GET /api/dashboard/mcp-keys/tools` (grouped by the
same 19 domain groups the key-creation UI offers) or `mcp_auth.TOOL_GROUPS`:

| Group | Group | Group |
|---|---|---|
| Core | vSphere | Octopus Deploy |
| Cross-domain host context | Rapid7 / vulnerabilities | Host posture (PCI) |
| OS inventory | Network / cloud / k8s | GitLab / IaC / CI-CD |
| Governance / compliance | Internal governance | Knowledge / relationship graph |
| Manual-write provenance | Collection control | Resource seeding |
| Governance actions | Graph identity confirmation | Reasoner writes |
| Batch closure | | |

---

## Query Tools

All query tools are read-only and return a list of dicts (one per row).
UUID and datetime fields are serialized to strings.

---

### `query_resources`

Return collected infrastructure resources.

| Param | Type | Default | Description |
|---|---|---|---|
| `domain` | `str` | `None` | Filter by domain (`cicd`, `linux`, `windows`, `octopus`, …) |
| `type` | `str` | `None` | Filter by resource type (`gitlab_project`, `host`, …) |
| `limit` | `int` | `50` | Max rows returned |

**Returns:** `[{id, domain, type, name, source, zone, last_seen, metadata_}, …]`

---

### `get_drift_events`

Return config drift events, joined to the resource name.

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | `str` | `open` | `open`, `acknowledged`, `resolved`, or `""` for all |
| `hours` | `int` | `24` | Only events detected within the last N hours |
| `domain` | `str` | `None` | Filter by resource domain |
| `limit` | `int` | `100` | Max rows returned |
| `offset` | `int` | `0` | Row offset for paging |
| `include_graph_maintenance` | `bool` | `False` | Include graph-maintenance drift rows |
| `has_note` | `bool` | `None` | `True`/`False` to filter on presence of a root-cause note |

**Returns:** `[{id, resource_id, drift_type, field, old_value, new_value, detected_at, status, resource_name, resource_domain}, …]`

---

### `get_vulnerabilities`

Return CVEs from the vuln queue, joined to the affected host name.

| Param | Type | Default | Description |
|---|---|---|---|
| `severity` | `str` | `None` | `critical`, `high`, `medium`, `low` |
| `status` | `str` | `open` | `open`, `resolved`, or `""` for all |
| `limit` | `int` | `50` | Max rows |
| `offset` | `int` | `0` | Row offset for paging |

**Returns:** `[{id, cve_id, kb_id, severity, sla_due, status, host}, …]`

---

### `get_eol_status`

Return EOL registry entries sorted by EOL date ascending.

| Param | Type | Default | Description |
|---|---|---|---|
| `days_until_eol` | `int` | `None` | Only assets with EOL within N days |
| `limit` | `int` | `50` | Max rows returned |

**Returns:** `[{id, asset_name, eol_date, pci_risk_score, migration_path, resource_name}, …]`

---

### `get_remediation_suggestions`

Return proposed remediations from the RemediationAgent.

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | `str` | `pending` | `pending`, `approved`, `executed`, or `""` for all |
| `limit` | `int` | `50` | Max rows returned |
| `offset` | `int` | `0` | Row offset for paging |

**Returns:** `[{id, agent, action_type, target, payload, confidence, status, result_url}, …]`

---

### `get_inventory_gaps`

Return hosts discovered in real infra but missing from the Ansible inventory.

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | `str` | `proposed` | `proposed`, `merged`, or `""` for all |
| `limit` | `int` | `50` | Max rows returned |
| `offset` | `int` | `0` | Row offset for paging |

**Returns:** `[{id, host, domain, target_group, status, mr_url, detected_at}, …]`

---

### `get_instincts`

Return learned operational patterns from the knowledge base.

| Param | Type | Default | Description |
|---|---|---|---|
| `domain` | `str` | `None` | Filter by domain |
| `zone` | `str` | `corpor` | `corpor` or `hsa` |
| `min_confidence` | `float` | `0.7` | Minimum confidence threshold |
| `limit` | `int` | `50` | Max rows returned |

**Returns:** `[{id, zone, domain, pattern, confidence, promoted_by, promoted_at, citation}, …]`

---

### `get_collection_health`

Return recent collection run results — domain, status, resource and drift counts.

| Param | Type | Default | Description |
|---|---|---|---|
| `hours` | `int` | `24` | Only runs started within the last N hours |
| `limit` | `int` | `100` | Max rows returned |

**Returns:** `[{id, domain, trigger_type, started_at, finished_at, resources_found, drift_count, status}, …]`

---

### `query_nl`

Answer natural-language questions about the infrastructure database using SQL,
via the read-only `QueryAgent`.

| Param | Type | Description |
|---|---|---|
| `question` | `str` | Natural-language question about the infra database |

**Returns:** the `QueryAgent`'s structured answer dict, or `{error: <message>}` if
`QueryAgent` is unavailable in this deployment.

---

### `search_knowledge`

Semantic search over the internal knowledge base (RAG) — runbooks, decision
records, architecture docs, and other indexed sources.

| Param | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | required | Search text |
| `k` | `int` | `5` | Number of top results to return |

**Returns:** `[{..., similarity}, …]` — top-k chunks with provenance (title, space,
url) and a numbered `similarity` score to cite. An empty list means the knowledge
base has nothing on that topic (or RAG is disabled).

---

## Management Tools

Management tools write to Infra Brain's own database. **Two independent gates apply
to every one of them:** the calling key must list the tool in its `allowed_tools`
(else `403 key_lacks_scope`, before dispatch), *and*
`INFRA_BRAIN_MCP_ENABLE_MUTATIONS` must be truthy (else the tool returns
`{"error": "mutating MCP tools are disabled; …"}` with HTTP 200). Scope alone is not
enough, and neither is the flag alone.

The RCE-risk tools that used to live here — `set_secret`, `update_config`,
`deploy_agent`, `get_agent_logs` — were **removed outright** (F-025). The MCP
container consequently mounts `.env` read-only and gets no Docker socket. To change
a secret, edit `.env` and redeploy; to ship an agent, rebuild and redeploy.

---

### `trigger_collection`

Trigger an immediate collection sweep for a domain.

| Param | Type | Default | Description |
|---|---|---|---|
| `domain` | `str` | required | Domain to sweep: `cicd`, `octopus`, `linux`, `windows`, `inventory_reconcile`, `remediation`, etc. |
| `force` | `bool` | `False` | Pass `true` to bypass the Redis dedup lock |

**Returns:** `{status: <http_code>, body: <response_text>}` or `{error: <message>}`

---

### `approve_proposal`

Approve a pending `ProposedAction` — full parity with the dashboard route (guards are
shared verbatim with `action_decisions.approve_action`). With
`INFRA_BRAIN_MR_ENABLED=true` the RemediationAgent will open a GitLab MR on the next
collection run. Refuses, writing nothing, for an unknown action, a non-`pending` one,
`confidence < 0.7`, or an `entity_resolution_same_as` review row (those have their own
confirm path).

| Param | Type | Default | Description |
|---|---|---|---|
| `action_id` | `str` | required | UUID of the `proposed_actions` row |
| `approver_label` | `str` | `None` | Optional free-text hint. **Not the identity** — `approved_by` is derived server-side from the authenticated key and the label is appended as `mcp:<key name> (says: <label>)`. |

**Returns:** `{approved: <id>, target: <target_string>, approved_by: <derived_identity>, graph_resumed: <bool>}` or `{error: <message>}`

---

### `reject_proposal`

Reject a pending `ProposedAction` — mirror of the dashboard route
(`action_decisions.reject_action`). Sets `status='rejected'`; a rejected action is
never executed. Refuses, writing nothing, for an unknown or non-`pending` action.

| Param | Type | Description |
|---|---|---|
| `action_id` | `str` | UUID of the `proposed_actions` row |

**Returns:** `{rejected: <id>, target: <target_string>, graph_resumed: <bool>}` or `{error: <message>}`

---

### `promote_instinct`

Add a new learned pattern to the instincts knowledge base.

| Param | Type | Default | Description |
|---|---|---|---|
| `pattern` | `str` | required | The operational pattern / claim |
| `domain` | `str` | required | Domain tag (`cicd`, `windows`, `linux`, …) |
| `citation` | `str` | required | Source document or evidence for the pattern — rejected if blank |
| `approved_by` | `str` | required | Who approved it — rejected if blank. Stored verbatim as `promoted_by` (unlike `approve_proposal`, this field is caller-supplied, not key-derived) |
| `zone` | `str` | `corpor` | `corpor` or `hsa` |
| `confidence` | `float` | `0.8` | Confidence score, must satisfy `0 < confidence <= 1` |

**Returns:** `{promoted: <id>, domain: <domain>, confidence: <float>}` or `{error: <message>}`

---

### `add_eol_product`

Register a product in the EOL registry. Upserts by `asset_name`, so a manual entry
merges with an auto-derived one instead of duplicating it. Creates a synthetic
`Resource` row (`domain='eol'`, `source='mcp'`) if `resource_id` is not supplied.

| Param | Type | Default | Description |
|---|---|---|---|
| `asset_name` | `str` | required | Product name (e.g. `"PostgreSQL 15"`) |
| `eol_date` | `str` | required | ISO date `YYYY-MM-DD` |
| `pci_risk_score` | `int` | `None` | PCI risk score, 0–100 (see `EOLAgent._pci_risk_score`: past-EOL=90, <90 days=70, <1yr=40, else=10). NOT the same scale as Rapid7's `risk_score` field (an unbounded exposure score, typically in the hundreds/thousands) — do not conflate the two. |
| `migration_path` | `str` | `None` | Suggested migration path |
| `resource_id` | `str` | `None` | Link to an existing `resources` row (UUID) |

**Returns:** `{registered: <id>, …}` on insert, `{updated: <id>, …}` on merge — each
with `asset: <name>, eol_date: <date>` — or `{error: <message>}`

---

### Other mutating tools

Beyond the five above, the write-scoped set also covers the seeding tools (below),
graph identity confirmation (`confirm_same_as`, `retract_same_as`), the manual
reasoner writes (`record_rootcause_note`, `record_rootcause_notes_bulk`,
`record_compliance_gap` — every row they write is permanently stamped
manual/MCP-authored), predicate-scoped batch closure (`resolve_drift_events`,
`close_compliance_violations`) and bulk proposal decisions
(`bulk_approve_proposals`, `bulk_reject_proposals`). The batch/bulk tools require an
explicit narrowing predicate, preview by default (`dry_run=True`) and cap rows per
call. `mcp_auth.MUTATION_TOOL_NAMES` is the authoritative list — render it with
`mcp_auth.write_scope_tool_table()` rather than copying it anywhere.

---

## Seeding Tools

Seeding tools manually populate the database with resources, drift events, and
vulnerabilities — for pre-populating hosts/assets before a collector comes
online, or for data you don't want a full collector for. They are subject to both
gates described under Management Tools above: write scope on the key *and*
`INFRA_BRAIN_MCP_ENABLE_MUTATIONS`.

The one exception is `get_seeded_resources`: it is a pure read and has no
`INFRA_BRAIN_MCP_ENABLE_MUTATIONS` check, but it is listed in
`MUTATION_TOOL_NAMES`, so a key still needs write scope to call it. Scope a
read-only key from `.readonly` in the tool catalog and it will *not* include this
tool.

---

### `seed_resource`

Manually seed a single `Resource` (upsert by `domain` + `resource_type` + `hostname`).

| Param | Type | Default | Description |
|---|---|---|---|
| `hostname` | `str` | required | Resource name |
| `domain` | `str` | required | `linux`, `windows`, `vsphere`, `octopus`, `iac`, `eol`, `manual`, … |
| `resource_type` | `str` | `host` | `host`, `vm`, `project`, `file`, `product`, `device` |
| `ip_address` | `str` | `None` | IP address, stored in metadata |
| `os_name` | `str` | `None` | OS name, stored in metadata |
| `environment` | `str` | `None` | Environment tag, stored in metadata |
| `tags` | `list` | `None` | Free-form tags, stored in metadata |
| `metadata` | `dict` | `None` | Extra metadata merged into the resource's `metadata_` |
| `source` | `str` | `manual` | Source label |

**Returns:** `{resource_id: <uuid>, created: <bool>, hostname: <name>}` or `{error: <message>}`

---

### `seed_resources_bulk`

Bulk-seed multiple resources from a YAML string (each item matching
`seed_resource`'s parameters).

| Param | Type | Description |
|---|---|---|
| `resources_yaml` | `str` | YAML list of resource objects |

**Returns:** `{created: <count>, updated: <count>, errors: [{index, hostname, error}, …]}`

---

### `seed_drift_event`

Manually record a drift event for an already-seeded host.

| Param | Type | Default | Description |
|---|---|---|---|
| `hostname` | `str` | required | Must already exist as a `Resource` (seed it first) |
| `drift_type` | `str` | required | `config_drift`, `new_listening_port`, `service_stopped`, `policy_violation`, `manual_observation` |
| `field` | `str` | required | Field/attribute that drifted |
| `old_value` | `str` | `None` | Previous value |
| `new_value` | `str` | `None` | New value |
| `severity` | `str` | `medium` | `low`, `medium`, `high`, `critical` |
| `source` | `str` | `manual` | Source label |
| `note` | `str` | `None` | Free-text note |

**Returns:** `{event_id: <uuid>, resource_id: <uuid>}` or `{error: <message>}` — errors if the host hasn't been seeded yet.

---

### `seed_vulnerability`

Manually record a known CVE for an already-seeded host.

| Param | Type | Default | Description |
|---|---|---|---|
| `hostname` | `str` | required | Must already exist as a `Resource` (seed it first) |
| `cve_id` | `str` | required | CVE identifier |
| `severity` | `str` | required | `Low`, `Medium`, `High`, `Critical` |
| `description` | `str` | `None` | Stored in the resource's metadata (no dedicated column) |
| `solution` | `str` | `None` | Stored in the resource's metadata |
| `cvss_score` | `float` | `None` | Stored in the resource's metadata |
| `source` | `str` | `manual` | Source label |

**Returns:** `{vuln_id: <uuid>, upserted: <bool>}` or `{error: <message>}` — upserts by `(resource_id, cve_id)`.

---

### `get_seeded_resources`

List resources that were manually seeded (`source='manual'` by default).

| Param | Type | Default | Description |
|---|---|---|---|
| `domain` | `str` | `None` | Filter by domain |
| `source` | `str` | `manual` | Filter by source label |
| `limit` | `int` | `50` | Max rows returned |

**Returns:** `{resources: [{id, hostname, domain, type, ip, last_seen}, …], count: <total>}`

---

## Docker Compose Service

Base service (`docker/docker-compose.yml`) — production-safe, no live source mount
(abridged; see the file for the healthcheck and resource limits):

```yaml
mcp:
  image: ${CI_REGISTRY_IMAGE:-infra-brain}:${IMAGE_TAG:-latest}
  build:
    context: ..
    dockerfile: docker/Dockerfile
  command: python -m infra_brain.mcp_server
  # Bind on the real interface so infra-ops can reach MCP across LAN subnets.
  # Access control is per-key scoped auth, NOT loopback binding.
  ports:
    - "8002:8002"
  env_file:
    - ../.env
    - path: /opt/infra-brain/collector-secrets.env
      required: false
  environment:
    POSTGRES_URL: ${DATABASE_URL}
    REDIS_URL: redis://redis:6379/0
    INFRA_BRAIN_DEV: ${INFRA_BRAIN_DEV:-false}
    ENVIRONMENT: ${ENVIRONMENT:-development}
    INFRA_BRAIN_MCP_ENABLE_MUTATIONS: ${INFRA_BRAIN_MCP_ENABLE_MUTATIONS:-}
  volumes:
    - ../.env:/app/infra-brain.env:ro
  depends_on:
    seed:
      condition: service_completed_successfully
    redis:
      condition: service_healthy
  restart: unless-stopped
  healthcheck:      # probes the no-auth /healthz, NOT the auth-gated /mcp
    test: ["CMD", "python", "-c", "import urllib.request,sys; ..."]
```

Dev overlay (`docker/docker-compose.dev.yml`) adds the live-source mount and rebinds
the port to loopback — apply both files together for local development
(`docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up`):

```yaml
mcp:
  volumes:
    - ../src:/app/src
  ports:
    # F-025: keep MCP localhost-only under the dev overlay.
    - "127.0.0.1:8002:8002"
```

| Mount | Purpose |
|---|---|
| `../.env:/app/infra-brain.env:ro` | Read-only. Nothing in the MCP container rewrites secrets — `set_secret`/`update_config` were removed. |
| `../src:/app/src` (**dev overlay only**) | Hot-mounts source for local dev. Not mounted in production. |

> **Note:** there is **no** `/var/run/docker.sock` mount. The restart-orchestration
> and log-fetching tools were removed, so the container needs no host control.

---

## Troubleshooting

### `401 Unauthorized` — `{"reason": "key_invalid_or_expired"}`

Either no `Authorization: Bearer …` header at all, or the token hashes to no active
`mcp_api_keys` row (never existed, or was revoked). Tokens are not in the container's
environment any more — there is nothing to `grep` for. Check the key instead: list keys
on `/dashboard2/mcpkeys` (or `GET /api/dashboard/mcp-keys`) and confirm the one you are
using is present and not revoked, then compare `last_used_at` after a retry.

A common cause is a pre-2026-07-23 token: a bare hex string rather than `ibmcp_…`. That
value can never match a row and will 401 forever — mint a new key (see Authentication)
rather than trying to revive it.

### `403 Forbidden` — `{"reason": "key_lacks_scope"}`

The key is valid but the requested tool is not in its `allowed_tools`. For a JSON-RPC
batch the check is all-or-nothing across every `tools/call` entry, so one out-of-scope
name denies the whole batch. Amend the key's scope with `PATCH /api/dashboard/mcp-keys/{id}`
(or the dashboard) — no re-issue is needed and the token is unchanged.

### `403 Forbidden` — `{"reason": "unparseable_request"}`

The middleware could not classify the request body well enough to check tool scope, so
it failed closed. This means a malformed JSON-RPC body (bad JSON, a batch entry that
isn't an object, a `tools/call` with no string `params.name`), not an auth problem.

### Mutating tool returns `200` with `"mutating MCP tools are disabled"`

Key scope is fine — the process-wide gate is off. Set
`INFRA_BRAIN_MCP_ENABLE_MUTATIONS=true` for the `mcp` service and recreate it. Both
gates are required; scope alone never suffices.

### `406 Not Acceptable`

Two distinct causes, both from the MCP transport rather than infra-brain's own code:

- **On a plain GET** — expected; streamable-HTTP requires a proper MCP handshake, not
  a bare GET. A `406` from `curl` here means the server is running correctly.
- **On a real call** — the client sent an `Accept` header that does not include *both*
  `application/json` and `text/event-stream`. The response body is
  `Not Acceptable: Client must accept both application/json and text/event-stream`,
  emitted verbatim by the MCP SDK's `streamable_http.py`. Fix the client's headers;
  there is nothing to change server-side.

### `trigger_collection` → `error: Connection refused`

`INFRA_BRAIN_APP_URL` cannot reach the `app` container. Inside the compose network
`http://app:8000` resolves automatically. If running outside the network:
```
INFRA_BRAIN_APP_URL=http://192.0.2.13:8001
```

### `Unknown tool: set_secret` / `deploy_agent` / `get_agent_logs`

These tools no longer exist — they were removed as RCE risks (F-025) and are absent
from both `mcp_server.py` and `mcp_auth.ALL_TOOL_NAMES`. A key cannot even be created
naming one (`422 unknown tool name(s)`). Change a secret by editing `.env` and
redeploying; ship an agent by rebuilding and redeploying; read container logs with
`docker logs`.

### Container logs / names

`docker ps | grep docker-` — default compose names follow `docker-<service>-1` (not
`infra-brain-<service>-1`), so the MCP container is `docker-mcp-1`.
