# infra-brain Codebase Patterns

This document is read by AI agents at workflow step 1 before authoring any code.
It is derived from the live codebase — keep it accurate by updating it when patterns change.

> Source files this was extracted from (read them if a pattern is unclear):
> `src/infra_brain/api/routers/*.py` (route handlers — `dashboard_api.py` is now just a
> re-export shim), `src/infra_brain/db/session.py`, `src/infra_brain/db/models/*.py`
> (split by domain), `src/infra_brain/triage.py`, `src/infra_brain/agents/base.py`,
> `tests/conftest.py`, `tests/test_dashboard_api.py`, `dashboard-app/src/`.
>
> **Wave 6.1 B7 (DR-6.1):** The legacy DC-framework dashboard (`dashboard/src/`,
> `scripts/design_sync/build.py`, `src/infra_brain/dashboard/static/`) was retired.
> The Vite+React dashboard (`dashboard-app/`, served at `/dashboard2`) is now the
> sole frontend. References to `design_sync.build`, `index.html` as a committed
> artifact, or the `/dashboard` static mount are no longer valid.

---

## API Endpoints (`src/infra_brain/api/routers/*.py`)

Route handlers live in `api/routers/{cve,fleet,governance,hosts,iac,octopus,ui,vsphere,vuln}.py`
(each router owns its own routes + Pydantic schemas, mounted in `main.py`).
`dashboard_api.py` is now a 102-line re-export shim only — do not add handler logic there.

The dashboard API is **READ-ONLY**: every data route is a `GET`. The only `POST`
is `/chat` (the read-only assistant). Do not add mutating data endpoints here.

### Router definition

Routes hang off module-level `APIRouter` instances — **not** `@app.*`. There are
several routers, each with a distinct `prefix` and a shared session-auth gate:

```python
from fastapi import APIRouter, Depends
from infra_brain.dashboard_auth import require_session

router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# Purpose-built sibling routers, same session gate, distinct prefixes:
octopus_router = APIRouter(prefix="/api/octopus",  tags=["octopus"],  dependencies=[Depends(require_session)])
iac_router     = APIRouter(prefix="/api/iac",      tags=["iac"],      dependencies=[Depends(require_session)])
vsphere_router = APIRouter(prefix="/api/vsphere",  tags=["vsphere"],  dependencies=[Depends(require_session)])
```

`Depends(require_session)` is the **auth** dependency on the router. It is NOT the
DB session — see below. New dashboard endpoints go on `router`; a rich relational
estate that deserves its own response shape gets its own sibling router.

### Session pattern

The DB session is a `contextmanager`, used inline with `with get_session() as s:`.
It is **NOT** a FastAPI `Depends`. Defined in `db/session.py`:

```python
from contextlib import contextmanager
from sqlalchemy.orm import Session

@contextmanager
def get_session():
    with Session(get_engine()) as session:
        yield session
```

Endpoint usage — open the session inside the function body:

```python
@router.get("/resources", response_model=ResourcePageOut)
def list_resources(domain: Optional[str] = None, limit: int = 200, offset: int = 0):
    with get_session() as s:
        q = s.query(Resource)
        ...
```

> Do NOT write `def endpoint(s = Depends(get_session))`. `get_session` is a
> context manager, not a dependency — wiring it through `Depends` fails at startup.
> Tests monkeypatch `infra_brain.dashboard_api.get_session` (see Tests section),
> which only works because the route opens the session inline.

### Paginated response pattern

Paginated endpoints return a wrapper model with `items / total / limit / offset`.
The real example is `ResourcePageOut`:

```python
class ResourceOut(BaseModel):
    id: str
    hostname: str
    domain: str
    resource_type: str
    zone: str
    status: str
    last_seen_at: datetime
    drift_count: int = 0
    meta: list[KV] = []

class ResourcePageOut(BaseModel):
    items: list[ResourceOut]
    total: int
    limit: int
    offset: int
```

> As of the pagination-envelope migration, essentially every list endpoint returns the
> `{items, total, limit, offset}` wrapper — ~30 `*PageOut` classes subclass the shared
> `PageEnvelope` (`api/_envelope.py`), and `tests/test_contract_map.py` enforces this shape
> across all dashboard/octopus/iac/vsphere/graph routes. Only two intentional bare-list
> exceptions remain: `GET /software/vendors` and `GET /drift_events/domains`
> (`response_model=list[str]`) — both are flat option lists, not paginated collections.
> Match the existing endpoint you are extending; default to the envelope for any new list
> route.

### Endpoint skeleton

Complete minimal paginated endpoint following the real pattern:

```python
class WidgetOut(BaseModel):
    id: str
    name: str
    last_seen_at: datetime

class WidgetPageOut(BaseModel):
    items: list[WidgetOut]
    total: int
    limit: int
    offset: int

@router.get("/widgets", response_model=WidgetPageOut)
def list_widgets(domain: Optional[str] = None, limit: int = 200, offset: int = 0):
    with get_session() as s:
        q = s.query(Resource)
        if domain:
            q = q.filter(Resource.domain == domain)
        total = q.count()
        rows = q.order_by(Resource.last_seen.desc()).offset(offset).limit(min(limit, 1000)).all()
        items = [WidgetOut(id=str(r.id), name=r.name, last_seen_at=r.last_seen) for r in rows]
        return WidgetPageOut(items=items, total=total, limit=limit, offset=offset)
```

Note the `limit(min(limit, 1000))` cap — keep it; it is the convention for every
paginated query.

---

## Database

### Model imports

Models are imported by name from `infra_brain.db.models` (a package split by domain —
`core`, `rapid7`, `octopus`, `vsphere`, `cloud_k8s_net`, `ansible`, `os_inventory`,
`governance`, `host_posture`, `host_purpose` — all re-exported from
`db/models/__init__.py`, so imports below still resolve the same way). Import exactly
the classes you use:

```python
from infra_brain.db.models import (
    Resource,
    DriftEvent,
    VulnQueueItem,
    R7Vulnerability,
    HostIdentity,
)
```

`Base` is defined in `db/base.py` (re-exported from `db/models/`) — import it
from `infra_brain.db.models` in tests. The cross-dialect JSON column type is
`JSONB` (renders as PostgreSQL `JSONB`, falls back to JSON/TEXT on SQLite). Models
use SQLAlchemy 2.0 typed mappings: `Mapped[...]` + `mapped_column(...)`.

> The `Resource.metadata_` column maps to the DB column literally named
> `metadata` (`mapped_column("metadata", JSONB, ...)`) — the trailing underscore
> avoids clashing with SQLAlchemy's reserved `metadata` attribute. Use
> `r.metadata_` in Python.

### Common models agents need

**`Resource`** (`resources`) — the generic collected-resource row:
`id` (UUID), `domain` (str), `type` (str), `name` (str), `source` (str),
`zone` (str, default `"corpor"`), `last_seen` (datetime), `metadata_` (JSONB).

**`VulnQueueItem`** (`vuln_queue`) — the prioritized CVE queue:
`id` (UUID), `resource_id` (FK → resources.id), `cve_id` (str), `kb_id` (str|None),
`severity` (str), `sla_due` (datetime|None), `status` (str, default `"open"`),
`last_updated` (datetime). Unique natural key: `(resource_id, cve_id)`.

**`R7Vulnerability`** (`r7_vulnerabilities`) — a Rapid7 vuln definition keyed on
the upstream **slug** `r7_vuln_id` (e.g. `"microsoft-asp_net_core-cve-2023-36038"`),
unique + indexed. Shared across assets — **no FK to r7_assets**. Fields include
`severity`, `cvss_v3_score`, `cvss_v3_vector`, `cvss_v2_score`, `risk_score`,
`exploits` (int), `malware_kits` (int), `fix_available` (bool|None),
`pci_status` (str|None), `pci_fail` (bool|None), `published` (datetime|None),
`categories` (JSONB), `cves` (JSONB array), `details` (JSONB).

**`HostIdentity`** (`host_identities`) — cross-source canonical host record,
maintained by `HostReconcileAgent`. The merge key is `short_hostname`
(**normalized lowercase**, indexed, unique). Has five nullable `*_resource_id`
FKs (`r7_`, `vsphere_`, `octopus_`, `linux_`, `windows_`) plus denormalized
display fields: `fqdn`, `ip_addresses` (JSONB), `os_family`, `risk_score`,
`vuln_count`, `patch_status`, `vsphere_power_state`, `octopus_machine_status`,
`last_reconciled`.

> Audit rows live in **`AuditLog`** (`audit_log`), fields `agent`, `tool`,
> `input_hash`, … There is no `GovernanceEvent` model in this codebase. When in
> doubt about a model name, grep `db/models/` before importing.

### String-slug joins (no DB FK)

Rapid7 vulnerabilities are keyed by an internal slug (`r7_vuln_id`), but
`vuln_queue` keys by canonical `CVE-YYYY-N` ids — so they **do not join directly**.
Two bridge tables connect them by **string slug, not a DB foreign key** (you query
them directly; SQLAlchemy `relationship()` will not traverse these):

- **`R7VulnCve`** (`r7_vuln_cves`) — maps a vuln slug to each CVE it covers:
  columns `r7_vuln_id` (str, indexed) + `cve_id` (str, indexed); unique
  `(r7_vuln_id, cve_id)`.
- **`R7VulnSolution`** (`r7_vuln_solutions`) — many-to-many slug↔solution:
  `r7_vuln_id` + `r7_solution_id` (both str, indexed); unique together.

The full walk (string joins, performed by hand in a query):

```
vuln_queue.cve_id → r7_vuln_cves.cve_id → r7_vuln_id → r7_vulnerabilities
  (CVSS / exploits / PCI)
   → r7_vuln_solutions.r7_vuln_id → r7_solution_id → r7_solutions (fix steps)
```

Do not add ORM FK relationships across these — the codebase intentionally keys by
upstream string id so vulns/solutions can be shared across assets.

---

## Knowledge Graph (`resource_relationships`, `db/relationships.py`)

### Edge model

Every edge is a **directed** row: `(from_resource_id) --[relationship_type]-->
(to_resource_id)`. Read it as "from *verb* to" (e.g. `vm RUNS_ON esxi_host`,
`r7_asset VULNERABLE_TO cve_resource`). The controlled vocabulary is the
`RelationshipType` enum, grouped by domain:

- **Infra topology** — `RUNS_ON`, `MEMBER_OF`, `MANAGES`, `IN_DATACENTER`,
  `STORED_ON`, `ATTACHED_TO`, plus first-class hardware nodes `HAS_DISK`,
  `HAS_NIC`, `HAS_HBA`, `HAS_PHYSICAL_DISK`.
- **Deployment / CI-CD** — `DEPLOYED_TO`, `DEPLOYS_TO`, `TRIGGERED_BY`,
  `DEPENDS_ON`, and the MR-H bridge edge `DEPLOYED_VIA` (see below).
- **Vulnerability / patch** — `VULNERABLE_TO`, `PATCHED_BY`, `HAS_SOFTWARE`,
  `TAGGED_AS`, and the MR-H bridge edge `AFFECTED_BY` (see below).
- **IaC** — `BELONGS_TO`, `DEFINED_IN` (no longer written to *this* store —
  declared on `iac`'s `AgentSpec` with `EdgeDirection.INVERSE` and materialised
  into `graph_edges`; see `MIGRATED_TO_GRAPH_EDGES`), `PART_OF` (deferred — see
  KG-8 note below), `ANSIBLE_MANAGES`.
- **Compliance / EOL / drift** — `HAS_VIOLATION`, `RELATED_TO`, `RUNS_EOL`,
  `HAS_DRIFT`.
- **Identity** — `IS_SAME_AS` (see lifecycle below).

Each type has a paired `*Props` dataclass (e.g. `RunsOnProps`, `AffectedByProps`)
documenting the shape callers should put in the edge's free-form `properties`
JSONB bag — not DB-enforced, a lightweight reference kept honest by
`tests/test_declared_vs_emitted_edges.py`, which enumerates `RELATIONSHIP_PROPS`
against every type actually emitted in the codebase and accepts
`DEFERRED_RELATIONSHIP_TYPES` (`HOSTED_BY`, `PART_OF`) as intentional gaps.

`emit_edge` / `emit_edges_batch` upsert on the natural key
(`from_resource_id, to_resource_id, relationship_type`) — safe to call every
collection cycle; a re-emitted edge also reactivates a previously-archived one
(see lifecycle below). `get_neighborhood` walks the graph via a bounded,
cycle-safe recursive CTE (`_WALK_SQL`), capped by `depth` (hard ceiling 6),
`max_nodes`, `max_edges`, and an optional consumer-side `min_confidence` filter.

**KG-8 pointer:** the `PART_OF` deferral (`host PART_OF inventory_group` — no
Resource row exists for an Ansible inventory group) is explained in
`agents/iac.py`'s `_emit_iac_edges` docstring (currently ~lines 643–657); the
live `ANSIBLE_MANAGES` edges emitted in its place start at ~line 700
(inventory-group members) and ~line 733 (playbook-play targets). These line
numbers drift as `iac.py` changes — if this pointer goes stale again, grep
`_emit_iac_edges` rather than trusting the number.

### New edge families (MR-H)

- **`AFFECTED_BY`** — `software_resource AFFECTED_BY cve/vuln_resource`.
  Connects an installed-software Resource to the Rapid7 vulnerability whose
  title names that product (title-substring heuristic scoped to the same host,
  hence confidence < 1.0). Closes the host → software → CVE → solution
  traversal chain (KG-5). Emitter: `graph_maintenance._populate_typed_relationships`.
- **`DEPLOYED_VIA`** — `gitlab_project DEPLOYED_VIA octopus_project`. Bridges
  the CI subgraph (GitLab) to the CD subgraph (Octopus) by matching project
  names, completing the `ci_pipeline → project → environment → machine` path
  (name-match heuristic, confidence < 1.0) (KG-4). Emitter: same function.

### `IS_SAME_AS` lifecycle: decay → archive → hard-delete (MR-H)

`HostReconcileAgent` is the **sole writer** of `IS_SAME_AS` edges (cross-source
identity links — e.g. a Rapid7 asset + a vSphere VM + an Octopus machine all
being "the same host"). `GraphMaintenanceAgent._decay_confidence`
(`agents/graph_maintenance.py`) is the only thing that *ages* them, on every
`scope="all"` maintenance run:

1. An edge not refreshed (re-observed by a collector) within
   `IS_SAME_AS_DECAY_DAYS` (default 14 days) has its confidence multiplied by
   `_DECAY_FACTOR` (0.90) each pass.
2. Below `_DECAY_FLOOR` (0.50) the edge is no longer trusted as an *active*
   identity link but is **archived** (`status="archived"`), not deleted — it is
   excluded from `get_neighborhood` traversal (the `status = 'active'`
   predicate in `_WALK_SQL`) but stays queryable via the `/relationships` API
   for operator review or restore. This replaced an earlier behavior that
   **hard-deleted** at this same floor, permanently losing a
   potentially-still-valid link with no review step (the KG-6 data-loss fix) —
   any comment or assumption elsewhere that an `IS_SAME_AS` conflict is
   resolved by immediate deletion is stale.
3. Only below `_HARD_DELETE_FLOOR` (0.05) — i.e. decayed for many further
   cycles while still archived and unobserved — is a row ever actually deleted,
   bounding unbounded growth of archived rows.

A collector re-observing a previously-archived edge reactivates it: the
`ON CONFLICT` upsert in `emit_edge`/`emit_edges_batch` unconditionally sets
`status = 'active'`. Do not add a second `IS_SAME_AS` writer —
`GraphMaintenanceAgent`'s only remaining `IS_SAME_AS` responsibility is this
decay/archive/hard-delete maintenance, never emission of new links.

### Boot trap: library-managed DDL vs `compare_metadata` (N-5)

`db/schema_check.py::assert_schema_current` runs a full
`alembic.autogenerate.compare_metadata` diff of the live DB against
`Base.metadata` on every FastAPI startup (Postgres only) and raises
`SchemaDriftError` (process exits non-zero) on **any** difference — this is the
deploy-correctness gate that turns silent column drift into a loud failure (see
the module docstring for the production-500 incident that motivated it).

The trap: **any table created outside an Alembic migration trips this gate**,
even when the table's existence is entirely intentional. The concrete example
already in this codebase is `checkpointer.py`'s
`PostgresSaver`/`AsyncPostgresSaver.setup()` (LangGraph's own checkpoint
persistence), which creates `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`, and `checkpoint_migrations` directly. Those tables are not
declared in `infra_brain.db.models.Base.metadata` and no migration creates
them, so `compare_metadata` reports each one as `remove_table`/`remove_index`
drift and would otherwise block every deploy.

The fix pattern — copy this for any future library-managed table (a vector
store's own tables, a queue library's tables, etc.):

```python
# schema_check.py
_LANGGRAPH_MANAGED_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)

def _include_name(name, type_, parent_names):
    """compare_metadata's include_name hook — skip library-owned objects."""
    if type_ == "table":
        return name not in _LANGGRAPH_MANAGED_TABLES
    if type_ in ("index", "unique_constraint", "column", "foreign_key_constraint"):
        return parent_names.get("table_name") not in _LANGGRAPH_MANAGED_TABLES
    return True
```

passed as `MigrationContext.configure(conn, opts={"include_name": _include_name})`.
Both the runtime `assert_schema_current` gate **and** the CI `migration-parity`
job call `compare_metadata`, so a new library-managed table needs the exclusion
in both places (search for `_LANGGRAPH_MANAGED_TABLES`/`_include_name` before
adding a new one — do not maintain two divergent exclusion lists). This is a
different failure class from ordinary migration drift (a real column the model
declares but no migration ever created) — that class is fixed with a normal
Alembic migration, e.g. `alembic/versions/0026_reconcile_live_drift.py`, not
with an `include_name` exclusion.

---

## Helper Functions

### Available in `src/infra_brain/api/_helpers.py`

```python
def mask_secret(value: str | None) -> str        # "••••••1234"; "(unset)" if falsy
def _s(v: Any) -> str                             # None-safe str(); "" for None
def _now() -> datetime                            # timezone-aware UTC now
def _sla_string(due: datetime | None) -> str      # "due in 3d" / "overdue 2d" / "—"
```

Secrets are **never** returned raw — route any secret-shaped value through
`mask_secret()`.

### Shared vuln prioritization (`triage.py`)

`compute_vuln_priority` is the single source of truth shared by `VulnTriageAgent`
and the dashboard, so the agent and UI always agree on a CVE's priority:

```python
def compute_vuln_priority(
    severity: str | None,
    sla_due: datetime | None,
    pci_risk: int | None,
    now: datetime | None = None,
) -> int:
    # severity weight (critical=100/high=60/medium=30/low=10, default 10)
    # + SLA pressure (+50 overdue, +20 due within 7d)
    # + pci_risk * 4 ;  higher = more urgent

def is_high_priority(severity: str | None, sla_due: datetime | None, now=None) -> bool:
    # True if severity == "critical" OR sla_due is past
```

Always reuse these — do not re-implement priority scoring in a new endpoint or agent.

---

## Tests

### Fixture setup

`tests/conftest.py` isolates `Settings` from the developer's local `.env`
(autouse) and provides a session-scoped SQLite `engine` + a function-scoped `db`
Session. CI has no `.env`, so the suite runs on pure field defaults.

The dashboard API test (`tests/test_dashboard_api.py`) builds its own in-memory
engine and **monkeypatches `get_session`** because routes open the session inline.
This is the canonical pattern for testing an endpoint:

```python
@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return eng

@pytest.fixture
def client(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.dashboard_api import router
    app = FastAPI()
    app.include_router(router)
    with patch("infra_brain.dashboard_api.get_session", _get_session):
        yield TestClient(app)
```

Seed data with a real `Session(engine)` block (`s.add(...)`, `s.flush()` to get
generated ids before referencing them as FKs, then `s.commit()`).

### Asserting paginated responses

A paginated endpoint returns the wrapper object — assert on `items` / `total`,
**not** index `[0]` on the top level:

```python
resp = client.get("/api/dashboard/resources")
assert resp.status_code == 200
data = resp.json()
assert "items" in data and "total" in data
assert data["total"] == 1
assert len(data["items"]) == 1
row = data["items"][0]          # <-- index into items, not data
assert row["hostname"] == "prod-web-01"
```

For bare-list endpoints (`response_model=list[...]`) the JSON is a list, so
`data[0]` is correct there. Check the endpoint's `response_model` first.

---

## Frontend (Vite + React)

> **Wave 6.1 B7 (DR-6.1):** The legacy DC-framework dashboard was retired.
> `dashboard-app/` is now the **sole** frontend. Do NOT author DC-framework
> patterns (x-dc, sc-for, sc-if, renderVals, design_sync) — they are deleted.
>
> Authoritative reference: `dashboard-app/README.md` and `dashboard-app/src/api.ts`.

**One dashboard: `dashboard-app/`** — a Vite + React + TypeScript SPA served at
`/dashboard2` (and now also at `/` via redirect) from prebuilt static assets
(`src/infra_brain/dashboard/static2/`). The DC-framework shell and its build
pipeline (`scripts/design_sync/`, `dashboard/src/`, legacy `static/`) were deleted
in Wave 6.1 B7 ([DR-6.1](decisions/DR-6.1-dashboard-stack.md)).

`dashboard-app/` calls the same `/api/dashboard/*` / `/api/graph/*` / `/api/vsphere/*`
/ `/api/octopus/*` backend routes — no new backend surface. New pages go into
`dashboard-app/src/pages/` as React components wrapped in `PageBoundary`. Every
network call goes through `api.ts` (`apiGet`/`apiPost`) — never a raw `fetch()`.

**Build:** from `dashboard-app/`, run `npm run build` (Vite), which emits the
committed static bundle to `src/infra_brain/dashboard/static2/`. Commit the
`dashboard-app/` sources and the regenerated `static2/` artifact together. There
is no `design_sync` step and no CI design-sync-check gate — both were deleted
with the legacy DC dashboard.

## Agents (`ETLConnector` / `BaseAgent` subclasses)

Deterministic collectors subclass `ETLConnector` (`src/infra_brain/etl/base.py`)
**directly** — it owns the whole `run()` lifecycle (CollectionRun bookkeeping, the
timeout guard, generic Resource upsert + Snapshot writes, failure surfacing) and
deliberately imports neither `infra_brain.llm` nor `langgraph`, so a deterministic
collector is structurally unable to construct a model. `BaseAgent`
(`src/infra_brain/agents/base.py`) subclasses `ETLConnector` and adds **only** the
lazily-constructed `self.llm` property, for agents that reason with the model (see
`LLMAgent`/`reason()` below) — subclass `BaseAgent` only when the agent will
actually call `self.llm` somewhere; every other backward-compat name
(`CollectOutcome`, `CollectorSkipped`, `count_drift_events_for_run`) is
re-exported from `agents/base.py` too, so existing `from infra_brain.agents.base
import ...` call sites keep working unchanged.

A deterministic collector implements exactly **one** abstract method, `collect()`:

```python
import logging
from infra_brain.etl.base import ETLConnector, CollectOutcome, CollectorSkipped
from infra_brain.db.models import Resource          # + whatever detail models you need
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


class WidgetAgent(ETLConnector):
    domain = "widget"          # required: tags every Resource/CollectionRun row

    def collect(self, scope: str = "all") -> list[dict]:
        """Return a list of resource dicts: {name, type, data: {...}}."""
        if not self.settings.widget_enabled:
            raise CollectorSkipped("widget collection disabled")
        items = []
        # ... gather from the upstream source ...
        items.append({"name": "widget-01", "type": "widget", "data": {"k": "v"}})
        return items
```

Key facts from the real base classes:

- `collect()` may return a plain `list[dict]` shaped `{name, type, data}` (legacy —
  treated as an all-ok outcome), or a `CollectOutcome(items=..., errors=[...],
  count_override=None)` for the R3/F-007 partial-failure contract: any swallowed
  per-item failure **must** be appended to `.errors`, never silently dropped.
  `run()` derives `CollectionRun.status` from this: no errors -> `"completed"`;
  errors + some data -> `"partial"`; errors + no data -> `"failed"`. Raise
  `CollectorSkipped` for an intentional no-op (missing config, feature disabled) —
  recorded as `status="skipped"`, distinct from both `"completed"` (ran, found
  nothing) and `"failed"` (runtime error).
- `run()` does the `Resource` upsert (keyed on `domain + type + name`) and
  Snapshot write for every item for you.
- The LLM is **lazy** (`self.llm` property, only present on `BaseAgent`
  subclasses — plain `ETLConnector` subclasses have no `self.llm` at all).
  Deterministic collectors never touch it, so they need no model credentials.
  LLM agents set `llm_role` and call `self.llm` only in their reasoning path
  (`reason()` — see below).
- Rich relational collectors that write **detail tables** (octopus, iac, vsphere)
  do so in a second phase via `self._write_details(result, fn)` so a detail-write
  failure flips the run to `status="failed"` instead of silently reporting success.
  Use `self._upsert_detail(session, Model, row, key_fields=[...])` for natural-key
  detail upserts. The caller owns the transaction and must `session.commit()`.
- `run()` already opens its own `get_session()` blocks — do not open sessions in
  `collect()` unless you specifically need to read existing rows.

### LLM agents: `LLMAgent.reason()` (`agents/llm_base.py`)

Agents that call the model to decide what to do (rather than deterministically
collecting) subclass `LLMAgent` (itself a `BaseAgent`) and call
`self.reason(task, tools)`. `reason()` is not a hand-rolled loop — it drives
LangChain's `create_agent` (the LangGraph prebuilt ReAct successor), so every LLM
agent gets, for free:

- **Tool gating.** Every tool is wrapped by `_gate_tools()` before being handed to
  `create_agent`, so `enforce_tool_gate()` (the R2 read-only/DLP boundary gate)
  runs before the tool body executes — necessary because the framework's tool
  node calls a tool's `.func`/`.coroutine` directly and never goes through
  `LLMAgent._run_tool`.
- **Checkpointing, conditionally.** A tool-less `reason()` call (a single model
  turn) skips the checkpointer entirely — nothing ever resumes a fresh
  single-turn thread, so persisting one would be write-only garbage. Tool-using
  calls get `checkpointer.get_checkpointer()`: a shared, process-wide sync
  `PostgresSaver` wrapped in a `threading.RLock` around every public method,
  because APScheduler runs multiple domain-agent `reason()` loops concurrently
  against one shared `psycopg.Connection` (see `checkpointer.py`'s module
  docstring). The chat graph uses the separate `get_async_checkpointer()`
  instead — the sync `PostgresSaver` does not implement the async checkpoint API.
- **PAN redaction before persistence.** Every model turn is logged to
  `AgentDecisionLog` via `_log_decisions()`, which runs the raw model text
  through `redact_pans()` (Luhn-validated) **before** writing `reasoning_text` —
  never store unredacted model output.
- **Recursion-limit handling.** `max_iters` (default 8) bounds model turns
  (`recursion_limit = 2 * max_iters + 1`); on `GraphRecursionError` the last
  available model text is returned (parity with the old hand-rolled loop) and a
  `RECURSION_LIMIT_MARKER` decision row is logged so anomaly detection can flag a
  runaway agent.

Tools called from a `reason()` loop **must** raise `ToolException` (see below) —
a bare exception crashes the loop mid-iteration.

### @tool functions must raise `ToolException`, never bare exceptions

Any function decorated with `@tool` (i.e. a LangChain tool registered on an agent)
**must** raise `langchain_core.tools.ToolException` for all user-visible error
conditions — never bare `ValueError`, `RuntimeError`, or `Exception`. Bare exceptions
crash the `reason()` loop and bypass the DLP/Audit callback chain.

```python
from langchain_core.tools import ToolException, tool

@tool
def my_tool(args: str) -> str:
    """..."""
    if not valid:
        raise ToolException("validation error: ...")   # correct
        # NOT: raise ValueError("...")                 # wrong — crashes reason() loop
```

Source: `src/infra_brain/tools/ansible.py` (fixed in MR !89/`fix/tools/ansible`);
`src/infra_brain/agents/query.py` (`langchain_core.tools` import confirmed post-MR !87).

### HTTP helper retry pattern (tenacity)

All outbound HTTP GET helpers in `tools/rapid7.py`, `tools/octopus_tool.py`, and
`tools/gitlab.py` use `@retry` from `tenacity` to handle transient 429/5xx errors.
The standard pattern:

```python
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

def _is_transient_http(exc: BaseException) -> bool:
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False

@retry(
    retry=retry_if_exception(_is_transient_http),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=1, max=30),
    reraise=True,
)
def _get(client, url, **kwargs):
    resp = client.get(url, **kwargs)
    resp.raise_for_status()
    return resp
```

Key invariant: **never retry 4xx errors other than 429** — retrying auth failures
(401/403) or not-found (404) produces misleading noise. Source: `tools/rapid7.py`,
`tools/octopus_tool.py`, `tools/gitlab.py` (added in MR !94/`fix/http-retry-tenacity`).

### Octopus ETL PAN masking

`agents/octopus.py` applies Luhn-validated PAN masking to every Octopus API response
record **before** it reaches the ORM or the dashboard. The entry point is
`_mask_record(record)` which calls `_mask_pan_shapes()` recursively (strings,
dicts, lists). Matching digit sequences that pass Luhn validation are replaced with
`[PAN-MASKED]`; a WARNING is logged (field count only, no values). This is
transparent to callers — the masked dict is returned and written to the DB.

If you add a new Octopus API fetch, wrap the raw response through `_mask_record()`
before upserting. Do not weaken or bypass the Luhn check. Source: `agents/octopus.py`
lines 61–128 (added in MR !91/`fix/octopus-pan-shape-masking`).

### Skipping domains without error (`collection_disabled_domains`)

`config.py` exposes `collection_disabled_domains: str = ""` (env:
`COLLECTION_DISABLED_DOMAINS`). Set it to a comma-separated list of domain names
(e.g. `"vsphere,windows"`) to skip those collectors during scheduled runs without
surfacing an error. The base agent checks this before calling `collect()`. Use this
for planned maintenance windows, not permanent disables. Source: `config.py` line 194,
`agents/base.py` (added in MR !95/`feat/collection-disabled-domains`).

### Agent registration: the `AgentSpec` declarative registry (TRK-041 / TRK-047)

Every agent class declares one frozen dataclass as a class attribute —
`spec = AgentSpec(...)` (`spec: ClassVar[AgentSpec]`, defined in
`src/infra_brain/etl/spec.py`). It is the **single source of truth** for a domain's
`domain`, `tier`, `schedule` (cron string or `None`), `max_staleness` (freshness
window or `None`), `skip_hook`, and `dispatchable`:

```python
class NetDiscoveryAgent(BaseAgent):
    spec = AgentSpec(
        domain="netdiscovery",
        tier=Tier.COLLECTOR,
        schedule="*/15 * * * *",
        max_staleness=timedelta(hours=1),
        skip_hook=True,
    )
```

This replaced the old hand-maintained parallel dicts — `scheduler`'s
`DEFAULT_SCHEDULES`, `coverage.DEFAULT_SCHEDULES`, `fleet_health._DOMAIN_MAX_AGE`,
and `freshness.DOMAIN_EXPECTED_MAX_AGE` — which had already drifted out of sync
(octopus 24h vs 26h) before being collapsed to zero copies (TRK-047).
`ETLConnector.__init_subclass__` derives the legacy class attributes
(`domain`/`schedule`/`skip_hook`/`dispatchable`) from `spec`, so `supervisor.py`
and `scheduler.py` read them unchanged; the shadow-table consumers now derive from
specs via helpers (`schedule_by_domain()`, `max_staleness_by_domain()`).

`AGENT_REGISTRY` (`supervisor.py`) is built from the collected specs — **27
dispatchable domains** at present. cloud, k8s, and net are POC-disabled: cloud/k8s
keep `dispatchable=False` on their spec, and all three are removed from
`_AGENT_SPECS` so they drop out of the registry (no schedule, no sweep membership,
no freshness monitoring, no roster entry) until re-enabled.

> `AGENTS.md` is **generated** from the specs by `scripts/gen_agents_md.py` — never
> hand-edit it. The CI staleness test `tests/etl/test_agent_spec.py` fails when the
> checked-in file diverges from what the specs would produce. To change the roster,
> edit an agent's `spec` (or the generator) and regenerate.

### `netdiscovery`: a catalogued divergence from the collector pattern (TRK-062)

`NetDiscoveryAgent` (`agents/netdiscovery.py`) is a variant of the standard
collector pattern — fetch → classify → upsert a `Resource` plus domain-specific
rows (`NetDiscoveryHost`/`NetDiscoveryService`) → emit `DriftEvent`s — but with two
**deliberate** divergences worth knowing before you use it as a template:

1. **It subclasses the older `BaseAgent`, not `ETLConnector` directly, and overrides
   `run()`.** Instead of implementing `collect()` and letting the base `run()` drive
   the lifecycle, it manages the `CollectionRun` record itself across three tiers
   (Tier 0 passive DB/DNS harvest, Tier 1 active host sweep, Tier 2 active
   service/OS scan). Each tier has its own try/except so a tier failure marks the
   run `failed` with a specific `error_message` — per-tier partial-failure semantics
   the generic `collect()`→`CollectOutcome` contract does not express. (`collect()`
   still exists but returns `[]` and is unused.)
2. **It deliberately does NOT emit `IS_SAME_AS` edges.** Even though it observes
   cross-source hosts, identity linking is left entirely to `HostReconcileAgent`,
   the sole writer of `IS_SAME_AS` (see the Knowledge Graph section). Its `_persist`
   docstring records this on purpose.

This entry is **cataloguing only** — migrating netdiscovery onto `ETLConnector` is a
separate design decision and out of scope here.

---

## Breaking Change Checklist

When changing a response model or endpoint signature:

- [ ] Update `response_model=` on the route decorator.
- [ ] Update all tests that assert the old response shape (search
      `tests/test_dashboard_*` for the endpoint path).
- [ ] If the shape changed from a bare list to a paginated wrapper (or vice
      versa), update every `data[0]` ↔ `data["items"][0]` assertion.
- [ ] Document in the MR description: "Breaking: X endpoint response changed from
      Y to Z".
- [ ] Update the companion frontend page in `dashboard-app/src/pages/` (React) if
      the endpoint feeds a page, then rebuild with `cd dashboard-app && npm run build`.

---

## Common Mistakes (confirmed failures from CI history)

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Using `@app.get` instead of `@router.get` | Route not registered / import error | Use the module-level `APIRouter` (`router`, `octopus_router`, …) |
| Using `Depends(get_session)` | TypeError at startup — `get_session` is a context manager, not a dependency | Open it inline: `with get_session() as s:` |
| Referencing `GovernanceEvent` | `ImportError`/`AttributeError` — no such model | Audit rows are `AuditLog`; grep `db/models/` for the real name |
| Not `.lower()` on a hostname lookup | 404 for an existing host | `HostIdentity.short_hostname` is normalized lowercase — lower the lookup key |
| Asserting `data[0]` on a paginated endpoint | `KeyError: 0` / wrong shape | Assert `data["items"][0]`; check the `response_model` first |
| Adding an ORM FK relationship across `r7_vuln_cves` / `r7_vuln_solutions` | Join returns nothing | They link by **string slug**, not DB FK — query the bridge directly |
| Using `r.metadata` instead of `r.metadata_` | `AttributeError` / SQLAlchemy reserved-name clash | The Python attr is `metadata_` (DB column is `metadata`) |
| Editing `static2/` directly | Build artifact overwritten on next `npm run build` | Edit `dashboard-app/src/`, then `cd dashboard-app && npm run build` |
| Using raw `fetch()` in dashboard-app | ESLint `no-restricted-globals` error in CI | Use `apiGet`/`apiPost` from `dashboard-app/src/api.ts` |
| Committing without running pytest | CI failure | Run `pytest tests/` before `git commit` |
