# Known Gaps

Honest list of things that are documented as open rather than fixed, tested, or
verified. Nothing here is hidden or resolved-but-unmentioned — these are the actual
current gaps.

- **Redis-down degradation is unverified.** The app should degrade gracefully
  (503/degraded responses) rather than crash when Redis is unreachable, but this
  hasn't been exercised end-to-end: stop Redis, hit every cache-touching endpoint,
  confirm no 500s, restart Redis, confirm recovery.
- **Rollback path is unverified against a real database.** Every Alembic migration
  is expected to have a working `downgrade()`, but a full downgrade-then-upgrade
  drill (`alembic downgrade -1` → verify the app starts cleanly on the older
  schema → `alembic upgrade head` → verify again) has not been run end-to-end.
- **Collection-health "skipped vs. broken" distinction needs a behavioral check.**
  The dashboard is intended to distinguish intentionally-unconfigured collectors
  (vSphere, Kubernetes, cloud, network, Windows — all off by default in a homelab
  deployment) from collectors that are actually failing, using each collection
  run's `status`/`error_message`. The underlying data model supports this; whether
  the dashboard surfaces it correctly hasn't been re-confirmed recently.
