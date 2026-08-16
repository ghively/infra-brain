# Decision: dashboard error-handling rollout scope + responsive/mobile CSS

**Context:** GitLab issue #91 (`systems-design`) found that 30 of 34
dashboard-app pages use an identical, unstyled `if (error) return <div
role="alert">...</div>` pattern that unmounts the entire page on any fetch
error, bypassing the already-shipped `GlobalErrorBanner`/`EndpointErrorBanner`
system. It also noted zero responsive/mobile CSS exists anywhere, with no
recorded decision on whether that is intentional.

## Decision 1: rollout scope for this pass

This pass (see `docs/superpowers/plans/2026-07-23-frontend-error-handling-
redesign.md`) built the shared fix — `usePageData` (Task 2) — and migrated
exactly ONE page, `Drift.tsx` (Task 3), as the reference implementation,
plus fixed `Graph.tsx`'s separately-noted inconsistent loading states (Task
4). The other ~33 pages using the old pattern are **not** migrated in this
pass. This mirrors how `Drift.tsx`'s own row-click→DetailDrawer pattern was
itself introduced ("reference implementation... follow this pattern on the
remaining ~9 pages") — ship the pattern once, proven against a real page
with real filters, before a wider mechanical rollout.

**Follow-up playbook for migrating a remaining page:** for each page still
using `if (error) return <div role="alert">...`:
1. Replace its manual `data`/`error`/`loading` state + `load`/`useEffect`/
   `useAutoRefresh` wiring with `usePageData(fetcher, deps)` (Task 2's hook).
2. Replace the early-return error `<div role="alert">` with an inline,
   non-blocking `role="status"` notice rendered ABOVE the page's existing
   content (see `Drift.tsx`'s post-migration shape for the exact JSX).
3. Replace any `xyz === null` loading check with the hook's `loading` field.
4. Add a page test file modeled on `Drift.test.tsx` (3 tests: renders on
   success, does not unmount on error, shows the inline notice on error).

No specific page is scheduled next — pick up per normal issue-driven
prioritization, not proactively, consistent with this project's general
posture against speculative batch rework absent a concrete trigger.

## Decision 2: responsive/mobile CSS remains explicitly out of scope

Zero responsive/mobile CSS exists in `dashboard-app` today (no media
queries, no viewport-relative layout beyond ordinary flex/grid that happens
to reflow). This is **not** an oversight from this pass forward — it is a
recorded, intentional decision: infra-brain's dashboard is an internal
operator tool, used from a desktop/laptop browser during incident response
and routine sweeps triage; no stated requirement or user request for mobile
support exists. Revisit only if a concrete need for mobile/tablet dashboard
access is raised — do not add responsive breakpoints speculatively.
