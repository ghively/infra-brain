# dashboard-app

The Vite + React + TypeScript rebuild of the infra-brain web dashboard, per
[DR-6.1](../docs/decisions/DR-6.1-dashboard-stack.md). It is the **sole**
infra-brain frontend — a legacy server-rendered dashboard was retired and
deleted when this app replaced it.

## Where this runs

Built assets are served by the FastAPI app at **`/dashboard2`**
(`src/infra_brain/main.py`, mounted from `src/infra_brain/dashboard/static2/`).
`/` redirects to `/dashboard2`; there is no legacy `/dashboard` mount anymore.

This app calls the existing backend: every request goes through the
`/api/dashboard/*`, `/api/graph/*`, `/api/vsphere/*`, `/api/octopus/*`, etc. routes.
It adds no new backend surface of its own beyond what those routes already expose.

## Local development

```bash
npm ci
npm run dev      # Vite dev server; proxies /api/* to http://localhost:8000
                  # (see vite.config.ts) — run the FastAPI app separately for data
```

To build the production bundle FastAPI actually serves:

```bash
npm run build     # tsc -b && vite build; outputs to ../src/infra_brain/dashboard/static2/
```

`npm run build` output is what `/dashboard2` serves in production. In this
public copy `src/infra_brain/dashboard/static2/` is gitignored — clone,
`npm ci && npm run build` once, and it appears locally; the backend mounts it
automatically if present (`main.py` skips the `/dashboard2` mount otherwise).

## The fetch chokepoint rule

**Every** network call in this app goes through `apiGet` / `apiPost` / `apiPostStream` /
`apiPostRawStream` in `src/api.ts` — never call `fetch()` directly anywhere else.
This is enforced by `oxlint`'s `no-restricted-globals` rule (`.oxlintrc.json`; `src/api.ts`
itself is the one allowed exception). The chokepoint exists so that:

- 401 responses uniformly throw `AuthRequired` (pages redirect to `/dashboard2/login`)
- non-2xx responses uniformly throw `ApiError` — no silent-empty-array failure mode
- the `{items,total,limit,offset}` envelope vs. bare-array response shape is normalized
  in exactly one place (`rows()`), not re-implemented per page

If you add a new page and it needs data, import from `../api`, not `fetch`.

## Structure

```
src/
├── App.tsx              # <BrowserRouter basename="/dashboard2"> + route table + nav shell
├── nav.ts                # Sidebar section/item config — single source of truth for nav
├── api.ts                # The fetch chokepoint (see above)
├── PageBoundary.tsx      # Per-page error boundary — one page crashing doesn't take down the shell
├── domainGroups.ts       # Heuristic domain -> category classifier (sweep-domain picker grouping)
├── components/
│   ├── Sidebar.tsx       # Renders nav.ts; active-route highlighting; badge counts from /counts
│   ├── ChatDrawer.tsx    # Persistent floating chat panel (mounted outside <Routes>)
│   └── SortTh.tsx        # Sortable column header, pairs with hooks/useSort.ts
├── hooks/
│   ├── useSort.ts        # Client-side per-page-of-rows sort (endpoints are server-paginated,
│   │                       not server-sorted — see the file's header comment)
│   └── useFocusTrap.ts   # Focus trap + return-focus for dialog-like panels (ChatDrawer)
└── pages/                # One file per routed page, named after its route (Hosts.tsx -> /hosts)
```

## Testing this app

`npx vitest run` (component/unit tests) and `npx tsc --noEmit` (type check)
are both expected to stay green — run before every commit. `npm run lint`
(oxlint) and `npm run build` (which also runs `tsc -b`) are the other two
checks worth running locally; `npm run build` additionally catches most
API-contract drift against `src/api.ts`'s call sites.

The served bundle (`src/infra_brain/dashboard/static2/`) is a build artifact,
not source — a source-only change has no effect until you rebuild.
