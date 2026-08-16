# openui-sync

## Purpose
Keeps the OpenUI custom-views feature in sync with infra-brain's live API surface and its
current frontend renderer. Run this whenever a new endpoint is added to `api/routers/`, a DB
model changes, or you suspect the component registry / system prompt has drifted.

> **Where the pieces live now (post DC-dashboard retirement).** The legacy DC frontend
> component library (`src/infra_brain/dashboard/openui/library.ts`/`.js`) is GONE. The
> frontend component library now lives in the Vite+React app:
> `dashboard-app/src/openui/OpenUIRenderer.tsx` — a `COMPONENT_REGISTRY` object mapping
> component names to React components, plus a parser that renders self-closing JSX-like tags
> (e.g. `<FleetStatCard title="Online hosts" value={42} color="green" />`) emitted by the
> LLM. The still-live BACKEND pieces are unchanged: the server-side prompt generator
> `src/infra_brain/openui/prompt.py`, the custom-views routes in `src/infra_brain/api/routers/ui.py`,
> and the `custom_views` table / `CustomView` model in `src/infra_brain/db/models/core.py`.

## When to run
- After any `api/routers/` change that adds or modifies a route (`dashboard_api.py` is now a re-export shim only, not where routes live)
- After a new Alembic migration that adds columns to exposed tables
- After adding a new domain agent (may expose new data)
- After adding or renaming an entry in `COMPONENT_REGISTRY` (OpenUIRenderer.tsx)
- Before starting a new sprint on the custom-views feature
- When users report "I asked for X but got no results"

## What the skill audits

### 1. Component ↔ Prompt Coverage
Reads the `COMPONENT_REGISTRY` in `dashboard-app/src/openui/OpenUIRenderer.tsx` and the
component descriptions in `src/infra_brain/openui/prompt.py`, and cross-checks them:
- **WIRED** — component is registered with a real render function (e.g. `FleetStatCard`)
- **STUB** — component is registered but renders a `PreviewStub` ("not yet wired to data")
- **PROMPT-ONLY** — described in `prompt.py` but has no entry in `COMPONENT_REGISTRY`
  (the LLM will emit a tag the renderer can't resolve → fallback card)
- **REGISTRY-ONLY** — in `COMPONENT_REGISTRY` but not described in `prompt.py`
  (the LLM never knows to emit it → dead component)

### 2. Endpoint Coverage
Reads every `@router.get` / `@router.post` in `src/infra_brain/api/routers/*.py` and reports
which data surfaces have a corresponding wired component vs. which are only reachable as raw
data. Reports COVERED / MISSING (with a scaffold suggestion) / EXCLUDED.

### 3. System Prompt Freshness
Checks that `src/infra_brain/openui/prompt.py` (the server-side prompt generator) reflects
the current registry — no stale component descriptions, no missing new ones. This is the
contract that tells the LLM which tags it may emit.

### 4. View Persistence Schema
If the `custom_views` table exists, confirms the `CustomView` ORM model in
`src/infra_brain/db/models/core.py` has no drift from the Alembic migration head, and that
the custom-views routes in `src/infra_brain/api/routers/ui.py` reference only real columns
(run `/validate-sql` if they carry raw SQL).

---

## Instructions

When invoked as `/openui-sync`, perform the following steps IN ORDER. Use parallel reads for step 1.

### Step 1 — Read the sources (parallel)
1a. Read `src/infra_brain/api/routers/*.py` in full (`dashboard_api.py` is now a
    re-export shim only — the routes live in `api/routers/`). Extract every route:
    method, path, query params, response_model fields.
1b. Read `dashboard-app/src/openui/OpenUIRenderer.tsx`. Extract every key of
    `COMPONENT_REGISTRY` and note whether each maps to a real component or a `PreviewStub`.
1c. Read `src/infra_brain/openui/prompt.py`. Extract the component names/descriptions the
    system prompt advertises to the LLM.
1d. If `custom_views` migration exists, check `alembic/versions/` for the migration and
    `src/infra_brain/db/models/core.py` for `CustomView`; check `api/routers/ui.py`.

### Step 2 — Build the gap report
- Cross-check `COMPONENT_REGISTRY` keys against `prompt.py` descriptions → PROMPT-ONLY and
  REGISTRY-ONLY mismatches.
- For each registered component, note WIRED vs STUB.
- For each endpoint, decide whether a wired component surfaces its data → MISSING gaps.

### Step 3 — Report findings

```
## openui-sync Report

### ✅ Wired components (N)
[list: component name — data source]

### 🟡 Stub components (N — registered but render PreviewStub)
[list: component name]

### ⚠️ Prompt/registry mismatches (N)
[PROMPT-ONLY: described to the LLM but not in COMPONENT_REGISTRY]
[REGISTRY-ONLY: in the registry but never advertised in prompt.py]

### ⚠️ Endpoints with no component (N)
[list: endpoint path | response fields | scaffold suggestion]

### 📝 custom_views schema (OK / DRIFT)
[CustomView model vs migration head; raw-SQL column check]
```

### Step 4 — Offer to scaffold a component
For each MISSING/STUB component, offer a ready-to-paste addition to
`dashboard-app/src/openui/OpenUIRenderer.tsx`: a React component function plus its
`COMPONENT_REGISTRY` entry (see template below).

### Step 5 — Offer to update prompt.py
If prompt/registry mismatches were found, offer to regenerate
`src/infra_brain/openui/prompt.py` so the advertised components match the registry.

---

## Scaffold Template (React registry entry)

New components are plain React functions receiving `props: Record<string, unknown>` (the
parsed tag attributes), registered by name in `COMPONENT_REGISTRY`:

```tsx
function DriftEventTable(props: Record<string, unknown>) {
  // props come from the parsed tag, e.g. <DriftEventTable domain="linux" limit={20} />
  const domain = String(props.domain ?? "");
  const limit = Number(props.limit ?? 20);
  return (
    <Card>
      {/* TODO: fetch /api/... for `domain` and render up to `limit` rows */}
      <div style={{ fontSize: 12, color: "#8896b3" }}>
        DriftEventTable — domain={domain} limit={limit}
      </div>
    </Card>
  );
}

// then register it (replacing the PreviewStub entry):
const COMPONENT_REGISTRY = {
  // …
  DriftEventTable,
};
```

After adding/wiring a component, add or update its description in
`src/infra_brain/openui/prompt.py` so the LLM knows the tag and its props.

---

## Notes
- The DC dashboard (`/dashboard`, `dashboard/src/**`, `static/index.html`, the
  `static/openui/library.ts` frontend library) has been retired/deleted. `/dashboard2`
  (the Vite+React app in `dashboard-app/`) is the sole UI. Do all frontend component work in
  `dashboard-app/src/`.
- The prompt generator path is `src/infra_brain/openui/prompt.py` — this backend piece is
  still live.
- The custom-views persistence layer (`custom_views` table, `CustomView` model,
  `api/routers/ui.py`) is still live.
