# Infra Brain Dashboard

This directory is the mount point for the built dashboard frontend. The
frontend source lives in `dashboard-app/` at the repo root; `npm run build`
there outputs to `static2/` (git-ignored — a build artifact, not committed,
and not present in this checkout until you build it).

## How it's served

`main.py` (`src/infra_brain/main.py`):
- redirects `/` → `/dashboard2`
- serves the built SPA (from `static2/`) at `/dashboard2`
- mounts the API router at `/api/dashboard/*`

`dashboard_api.py` (in `src/infra_brain/`) is a re-export shim only — the
real route handlers live in `src/infra_brain/api/routers/`.

Run the app, build the frontend once, then open `http://<host>:8000/dashboard2/`.
