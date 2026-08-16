# Design Sync — Phase 0 Render Spike Findings

**Date:** 2026-06-23
**Question:** Does `support.js` render `index.html` inside a bare browser (and thus
inside Claude Design) without extra `<head>` dependencies?

## Method

Loaded the real `src/infra_brain/dashboard/static/index.html` + `support.js` from a
temp dir in headless Chromium (playwright), captured console + pageerror events,
and checked that the dashboard chrome rendered.

## Result

- **Renders standalone.** `<aside>` and `<main>` are present; body text shows the
  real sidebar ("Infra Brain", OVERVIEW, Dashboard, Agents, INVENTORY, Resources,
  Drift Events, …). The DC runtime hydrates the component with its mock-data
  fallback. **No `window.React` / ReactDOM / Babel needed** — `support.js` is
  self-sufficient.

- **Decision: `transform.HEAD_INJECTIONS = []`.** `wrap()` is the identity
  function; the published artifact is byte-identical to the repo `index.html`.

## Important secondary finding (affects Task 6)

47 `console.error` messages fired, **all** of one benign class: unrendered
template holes inside SVG attributes during initial HTML parse, e.g.

```
<path> attribute d: Expected moveto path command ('M' or 'm'), "{{ nav.d }}".
<rect> attribute x: Expected length, "{{ b.x }}".
<line> attribute x1: Expected length, "{{ gl.x1 }}".
```

These occur because the browser parses the raw `<path d="{{ nav.d }}">` markup
*before* the DC runtime replaces the `{{ … }}` holes. They are pre-hydration
noise, not real errors (the June render-verify of the dashboard noted the same
class). **`render_verify.verify_url` must ignore console errors whose text
contains `{{` (template holes).** Filter rule: drop any console-error whose text
contains both `{{` and `}}`.

## Open / cannot test locally

- **Fonts / CSP:** Claude Design's CSP may block the external Google Fonts
  `<link>`. Cannot be tested locally (CSP is server-specific). A copy was published
  to the project (`_spike/`) for visual confirmation. Low risk: fallback is the
  system font stack. If blocked, the only consequence is the published preview
  uses fallback fonts — the served app is unaffected (it sets its own fonts).
