# DR-6.1 — Dashboard stack: bespoke DC framework vs boring standard stack

- **Date:** 2026-07-04
- **Status:** ACCEPTED
- **Roadmap item:** Wave 6 / 6.1
- **Findings:** F-011 (SRI hash brittleness), F-023 (renderVals blast radius / silent-empty fetch layer)
- **Decider:** A. Operator (youruser@example.com)

## Decision

**MIGRATE-TO-VITE-REACT**

## Metrics (from the Wave 6 migration-decision rules, Part 1 Step 2)

| Metric | Value | Rule threshold |
|---|---|---|
| M1 SRI/vendor churn commits since Wave 0.3 merge (2026-07-02) | 3 (58bcd6c, 38fb3d8, f803d4b) | migrate if ≥ 3 |
| M2 Wave 3 fixes hold (design_sync + vendored-assets suites green) | YES (3.1-3.4 gate PASSED, full suite green post-merge) | migrate if NO |
| M3 renderVals body raw line count | ~1712 | informational |
| M4 new pages planned next 6 months (maintainer answer, verbatim: "Many (6+)") | 6+ | migrate if ≥ 5 |
| M5 no-JS dashboard acceptable (maintainer answer, verbatim: "No, needs to stay JS-driven") | NO | server-render requires YES + explicit sign-off |

## Rule applied

Rule 3 of the migration-decision rules (Part 1 Step 3) matched first (M1 ≥ 3). Rule 4 (M4 ≥ 5) and the M5 = NO
exclusion of SERVER-RENDER both independently corroborate the same outcome.

## Consequences

The dashboard will be rebuilt as dashboard-app/ (Vite + React + TypeScript), ported
page-by-page behind /dashboard2 per the Wave 6 migration plan, Part 3
(sub-items B1–B7, one merge each). On completion of B7 the DC framework, design_sync
pipeline, vendored blobs, and SRI test machinery are deleted — F-011 and the F-023
structural risk cease to exist by construction.

---

## Addendum (2026-07-08): Visual Parity Scope

**Context:** DR-6.1 was scoped to technical/functional parity ("same data, same endpoints,
same interactions") and was silent on visual/pixel parity. During Wave 6 implementation
two divergences surfaced that warranted explicit decisions.

### Typography

The legacy dashboard (`dashboard/`) vendors DM Sans and DM Mono locally into
`dashboard/static/vendor/fonts/` with SRI pinning via `dm-fonts.css`. The initial
`dashboard-app/` scaffold shipped no vendored fonts, falling back silently to system
fonts — a visual divergence from legacy that was not intentional.

**Decision:** Re-vendor DM Sans and DM Mono locally into `dashboard-app/src/assets/fonts/`
(woff2 files, same source as legacy) with `@font-face` declarations in `index.css` and a
matching font-family stack on `body`. Implemented on branch
`fix/dashboard2-vendor-webfonts` (folded into the dashboard2 consolidation work). External CDN font
origins remain prohibited — the CSP and offline posture are unchanged; local vendoring only,
through the Vite asset pipeline (content-hashed, no runtime network fetch).

### Navigation / Information Architecture

The legacy sidebar has 9 sections and approximately 32 items. During the Wave 6 porting
work a deliberate UX audit (2026-07-06) reorganized the navigation to 7 sections and
approximately 24 items, documented in `dashboard-app/src/nav.ts`.

**Decision:** KEEP the reorganized navigation as-is. The change was deliberate and audited
— it is an improvement, not an error, and reverting it to match legacy pixel-for-pixel
would undo intentional UX work.

### Going-Forward Rule

- **Typography and color** must be preserved to match legacy (no silent divergence without
  an explicit product decision recorded here or in a successor ADR).
- **Information architecture and navigation structure** remain open to deliberate, audited
  redesign without requiring a new ADR, provided the rationale is captured in the relevant
  source file (e.g., `nav.ts`) or MR description.

---

## Addendum (2026-07-08): B7 Implementation Status

> **Re-verified 2026-07-13:** still pending — `main.py` redirects `/` → `/dashboard`
> (legacy remains the default) and the legacy static tree / `dashboard_api.py` shim
> still exist, confirming B7 has not merged. This addendum records the
> point-in-time decision only; see the final status below.

**Status: change authored; pending merge. Blocked on the dashboard2 consolidation work landing first.**

Branch `chore/retire-legacy-dashboard-b7` implements the B7 retirement per
the Wave 6 migration plan, Part 3. Once merged it will delete:

- `dashboard/src/` (shell.dc.html + 29 DC-framework page sources)
- `scripts/design_sync/` build pipeline (all except `check_no_external_origins.py` + `__init__.py`)
- `src/infra_brain/dashboard/static/` legacy artifact tree (index.html, support.js, openui/, vendor/)
- `tests/test_vendored_assets.py` and `tests/design_sync/` (all design-sync-specific tests)
- `design-sync-check` CI job, `design-sync-build` pre-commit hook, `build`/`design-check` Makefile targets
- `.gitattributes` vendored-blob binary attribute (no hand-vendored blobs remain)

Retained and updated:
- `scripts/design_sync/check_no_external_origins.py` — scanner updated to cover `static2/` and `dashboard-app/src/`
- `tests/test_no_external_origins.py` — scanner tests (moved from deleted `test_vendored_assets.py`)
- `src/infra_brain/main.py` — `/` redirect changed from `/dashboard` to `/dashboard2`; legacy `/dashboard` static mount removed

**F-011 closed on merge:** no SRI hash literals will remain anywhere in the codebase.
**F-023 closed on merge:** `renderVals` monolith does not exist in the Vite+React replacement; every page is wrapped in `PageBoundary`.

**DO NOT MERGE** the B7 branch until:
1. The `feat/dashboard2-full-consolidated` branch is merged to master AND verified working
   in the deployed container.
2. The B6 soak period (≥ 1 week of side-by-side use, maintainer sign-off per
   the Wave 6 migration plan) has completed. This soak clock cannot start until that
   branch is live.

**Rebase note:** The B7 branch was cut from master before the visual-parity addendum
(above) landed. After the consolidation branch merges, rebase B7 onto master so the two
addenda are contiguous in this file and there is no merge conflict.

---

## Addendum (2026-07-11): EOL-overdue badge color — accepted divergence

`Eol.tsx` renders the "overdue" status badge in red (`#f87171`), diverging from the
legacy dashboard's amber treatment for the same status. During this visual-parity pass
(`feat/dashboard2-visual-parity`) the option to revert to legacy amber was considered
and explicitly declined.

**Decision:** KEEP RED. Overdue is the most severe EOL state (see the existing
`eolBadge()` comment in `Eol.tsx`, which already documents the reasoning: red gives a
clearer "most severe" signal than reusing amber for both "overdue" and "approaching").
This is an intentional, accepted divergence from legacy, approved by the user
(A. Operator) during this pass — not a defect to fix in a future MR.

---

## Addendum (2026-07-16): B7 completed — legacy `/dashboard` deleted, `/dashboard2` is the sole UI

Wave 6.1 **B7 is complete** on master. The legacy `/dashboard` (DC-shell / DC-framework)
dashboard has been **retired and deleted** — `dashboard/src/**`, the committed
`src/infra_brain/dashboard/static/index.html` artifact tree, and the design-sync build
pipeline (`scripts/design_sync/build.py`, `sync.py`, `render_verify.py`, `transform.py`)
are gone. Only `scripts/design_sync/check_no_external_origins.py` remains, now scanning
`dashboard-app/`.

`/dashboard2` (the Vite+React SPA in `dashboard-app/`, built to
`src/infra_brain/dashboard/static2/`) is now the **sole UI**; `main.py` redirects
`/` → `/dashboard2`. The earlier "DO NOT MERGE" gates and side-by-side soak provisions in
the B6 addendum above are historical — they have been satisfied and superseded by this
completion.
