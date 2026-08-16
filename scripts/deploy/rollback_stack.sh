#!/bin/sh
# scripts/deploy/rollback_stack.sh
#
# Reusable "restore the previously-running app image" routine. It is the SINGLE
# implementation shared by two call sites in .gitlab-ci.yml so their behaviour
# can never drift apart:
#
#   (a) deploy: job  — the FAILED-HEALTH-CHECK auto-rollback path (TRK-006).
#                      Instead of leaving the host on a broken new stack with a
#                      bare `exit 1`, the deploy job calls this to redeploy the
#                      last-known-good image, then still exits 1 (deploy failed,
#                      but the host is recovered).
#   (b) rollback: job — the manual escape hatch (TRK-005 downgrade support).
#
# POSIX sh on purpose — the deploy/rollback jobs run on docker:24 (Alpine/
# busybox), which has NO bash. Keep this bash-free.
#
# It expects to run from the CI checkout root (docker-compose files + .env
# present), exactly like the inline job scripts it replaces.
#
# Overall sequence:
#   1. GUARDED, OPT-IN Alembic downgrade to the captured pre-deploy revision
#      (default OFF — see downgrade_guard.sh). Runs FIRST, using the NEW image's
#      migration scripts (only they contain the new migrations' downgrade()).
#   2. Redeploy the previous image (compose up --no-build).
#   3. Health-check the restored stack (liveness + /health readiness).
#
# Any failure returns non-zero so the calling job goes red.
set -eu

# ── Configuration (env-overridable; defaults mirror the deploy/rollback jobs) ──
COMPOSE_PROJECT="${COMPOSE_PROJECT:-infra-brain}"
COMPOSE_FILE_BASE="${COMPOSE_FILE_BASE:-docker/docker-compose.yml}"
COMPOSE_FILE_DEPLOY="${COMPOSE_FILE_DEPLOY:-docker/docker-compose.deploy.yml}"
PREV_IMAGE_MARKER="${PREV_IMAGE_MARKER:-/opt/infra-brain/previous_image}"
PREV_REV_MARKER="${PREV_REV_MARKER:-/opt/infra-brain/previous_revision}"
PG_VOLUME_MARKER="${PG_VOLUME_MARKER:-/opt/infra-brain/pg_volume}"
APP_CONTAINER="${APP_CONTAINER:-infra-brain-app-1}"
PG_CONTAINER="${PG_CONTAINER:-infra-brain-postgres-1}"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-24}"
# Image tag whose baked-in alembic scripts are used to compute/run a downgrade.
# For the deploy auto-rollback this MUST be the NEW (just-deployed) image — the
# new migrations' downgrade() functions live ONLY there. Defaults to the compose
# default 'latest' (the manual rollback job's usual case).
#
# KNOWN LIMITATION (HIGH #3, accepted / follow-up): the manual rollback job passes
# NEW_IMAGE_TAG=latest, but `build:` retags `latest` on EVERY master push, so by
# the time an operator runs a manual rollback, `latest` may point at a DIFFERENT
# image than the failed deploy — its downgrade() scripts could then differ from
# the ones that actually ran. The deploy AUTO-rollback is unaffected (it pins
# $CI_COMMIT_SHA). Proper fix = have the deploy capture the just-deployed image
# tag to its own marker (e.g. /opt/infra-brain/deployed_image) and have the
# rollback read it; deferred as human-owned follow-up (not done here to avoid
# widening this change). Downgrade is opt-in + OFF by default, which bounds the
# blast radius of this drift in practice.
NEW_IMAGE_TAG="${NEW_IMAGE_TAG:-latest}"
# Migration-downgrade guards — BOTH default OFF because downgrading is
# destructive-risk. Auto-rollback (deploy job) never sets these; the manual
# rollback job may pass them through from CI/CD variables.
ALLOW_MIGRATION_DOWNGRADE="${ALLOW_MIGRATION_DOWNGRADE:-0}"
ALLOW_DESTRUCTIVE_DOWNGRADE="${ALLOW_DESTRUCTIVE_DOWNGRADE:-0}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/downgrade_guard.sh"

# Thin wrapper so every compose call uses the identical project/env/files triple.
compose() {
  docker compose --project-name "$COMPOSE_PROJECT" --env-file .env \
    -f "$COMPOSE_FILE_BASE" -f "$COMPOSE_FILE_DEPLOY" "$@"
}

# Read the live DB's stamped alembic revision using the postgres container's OWN
# env (matches the deploy-state gate in .gitlab-ci.yml).
live_db_revision() {
  docker exec "$PG_CONTAINER" sh -c \
    'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-infra_brain}" -tAc "SELECT version_num FROM alembic_version"' \
    2>/dev/null | tr -d '\r' | head -1
}

# ── Step 1: guarded, opt-in migration downgrade ──────────────────────────────
# Returns 0 to CONTINUE the rollback, non-zero to ABORT (a guard refused).
maybe_downgrade_migrations() {
  TARGET=""
  if [ -f "$PREV_REV_MARKER" ]; then
    TARGET=$(tr -d '\r' < "$PREV_REV_MARKER" | head -1)
  fi
  CURRENT=$(live_db_revision || true)
  echo "Migration downgrade check: live DB revision='$CURRENT' captured pre-deploy revision='$TARGET'"

  # First decision pass with destructive=0 resolves the cheap branches
  # (opt-out / no-target / no-op / unknown-current) WITHOUT touching alembic.
  DEC=$(decide_downgrade "$CURRENT" "$TARGET" "$ALLOW_MIGRATION_DOWNGRADE" "0" "$ALLOW_DESTRUCTIVE_DOWNGRADE")
  case "$DEC" in
    SKIP_OPTOUT)
      # Downgrade is OPT-IN and disabled. If the schema actually moved, warn LOUDLY
      # and print the exact manual command — the previous image runs `alembic
      # upgrade head` on boot and will FAIL if the live DB is ahead of its head.
      if [ -n "$TARGET" ] && [ -n "$CURRENT" ] && [ "$CURRENT" != "$TARGET" ]; then
        echo "WARNING: live DB is at '$CURRENT' but the previous image expects '$TARGET'."
        echo "         Migration downgrade is OPT-IN and DISABLED (ALLOW_MIGRATION_DOWNGRADE!=1)."
        echo "         The previous image's startup migrate may FAIL against the newer schema."
        echo "         To downgrade MANUALLY (review for DATA LOSS first):"
        echo "           ALLOW_MIGRATION_DOWNGRADE=1 <re-run the rollback job>"
        echo "         or directly:"
        echo "           docker compose --project-name $COMPOSE_PROJECT --env-file .env \\"
        echo "             -f $COMPOSE_FILE_BASE -f $COMPOSE_FILE_DEPLOY \\"
        echo "             run --rm --no-deps migrate alembic downgrade $TARGET"
      fi
      return 0 ;;
    SKIP_NO_TARGET)
      echo "No captured pre-deploy revision marker ($PREV_REV_MARKER) — cannot downgrade; leaving schema as-is."
      return 0 ;;
    SKIP_NOOP)
      echo "Live DB already at captured revision '$TARGET' — no downgrade needed."
      return 0 ;;
    REFUSE_UNKNOWN_CURRENT)
      echo "ERROR: could not read the live DB revision — refusing to attempt a downgrade."
      return 1 ;;
  esac

  # DEC == DOWNGRADE candidate. Before running anything against the DB, generate
  # the OFFLINE downgrade SQL (no DB writes) and scan it for destructive DDL so
  # we can refuse data-loss downgrades unless explicitly accepted.
  echo "Generating offline downgrade SQL preview ($CURRENT -> $TARGET) to scan for destructive operations…"
  IMAGE_TAG="$NEW_IMAGE_TAG"; export IMAGE_TAG
  DESTRUCTIVE=0
  if DOWNGRADE_SQL=$(compose run --rm --no-deps migrate \
        alembic downgrade "${CURRENT}:${TARGET}" --sql 2>/tmp/downgrade_sql_err); then
    # Delegate the destructive verdict to the PURE, unit-tested scanner in
    # downgrade_guard.sh (sourced above). Any table/column drop -> "1".
    DESTRUCTIVE=$(printf '%s\n' "$DOWNGRADE_SQL" | scan_destructive_sql)
  else
    # Could not generate the preview (e.g. target not in the deploying image's
    # history, DB behind target, or env.py has no offline mode). We CANNOT verify
    # safety, so treat as destructive — this refuses unless the operator forces it.
    echo "WARNING: could not generate downgrade SQL preview; treating downgrade as DESTRUCTIVE (unverifiable)."
    sed 's/^/    alembic: /' /tmp/downgrade_sql_err 2>/dev/null || true
    DESTRUCTIVE=1
  fi

  # Second decision pass, now armed with the destructive verdict.
  DEC=$(decide_downgrade "$CURRENT" "$TARGET" "$ALLOW_MIGRATION_DOWNGRADE" "$DESTRUCTIVE" "$ALLOW_DESTRUCTIVE_DOWNGRADE")
  if [ "$DEC" = "REFUSE_DESTRUCTIVE" ]; then
    echo "REFUSING automatic downgrade: path $CURRENT -> $TARGET is DESTRUCTIVE (data loss) or unverifiable."
    echo "---- downgrade SQL preview (if any) ----"
    printf '%s\n' "${DOWNGRADE_SQL:-<none>}"
    echo "----------------------------------------"
    echo "Re-run with ALLOW_DESTRUCTIVE_DOWNGRADE=1 ONLY if you accept the data loss, or downgrade manually."
    return 1
  fi

  echo "Applying migration downgrade to '$TARGET' (destructive=$DESTRUCTIVE, explicitly allowed)…"
  # MEDIUM #6: do NOT let a failed downgrade APPLY abort the whole rollback under
  # `set -e`. If the apply fails partway, aborting here would skip
  # restore_previous_image + health_check, leaving NEITHER stack confirmed
  # healthy. Instead: capture the failure, WARN loudly with the manual command,
  # and still return 0 so main() proceeds to restore the previous image (the
  # operator finishes the schema by hand). The happy path (success) is unchanged.
  if compose run --rm --no-deps migrate alembic downgrade "$TARGET"; then
    echo "Migration downgrade to '$TARGET' complete."
  else
    echo "WARNING: migration downgrade to '$TARGET' FAILED partway — the schema may be"
    echo "         in an INTERMEDIATE state. Continuing to restore the previous image so"
    echo "         the host is not left down, but you MUST finish the downgrade manually:"
    echo "           docker compose --project-name $COMPOSE_PROJECT --env-file .env \\"
    echo "             -f $COMPOSE_FILE_BASE -f $COMPOSE_FILE_DEPLOY \\"
    echo "             run --rm --no-deps migrate alembic downgrade $TARGET"
  fi
  return 0
}

# ── Step 2: redeploy the previous image ──────────────────────────────────────
restore_previous_image() {
  [ -f "$PREV_IMAGE_MARKER" ] || { echo "ERROR: no $PREV_IMAGE_MARKER marker — nothing to roll back to"; return 1; }
  PREV_IMG=$(cat "$PREV_IMAGE_MARKER")
  docker image inspect "$PREV_IMG" >/dev/null 2>&1 \
    || { echo "ERROR: image $PREV_IMG not present in the host daemon"; return 1; }
  IMAGE_TAG="${PREV_IMG##*:}"; export IMAGE_TAG
  POSTGRES_VOLUME=$(cat "$PG_VOLUME_MARKER" 2>/dev/null || echo infra-brain_postgres_data); export POSTGRES_VOLUME
  echo "Restoring previous image $PREV_IMG (IMAGE_TAG=$IMAGE_TAG, POSTGRES_VOLUME=$POSTGRES_VOLUME)"
  compose up -d --no-build
}

# ── Step 3: confirm the restored stack is healthy + ready ────────────────────
health_check() {
  echo "Waiting for $APP_CONTAINER to become healthy (liveness) + ready (/health) after rollback…"
  i=1
  while [ "$i" -le "$HEALTH_ATTEMPTS" ]; do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$APP_CONTAINER" 2>/dev/null || echo missing)
    if [ "$status" = "healthy" ]; then
      if docker exec "$APP_CONTAINER" python -c "import json,sys,urllib.request; body=json.load(urllib.request.urlopen('http://localhost:8000/health', timeout=10)); sys.exit(0 if body.get('status')=='ok' else 1)"; then
        echo "Rollback healthy + ready after $((i * 5))s"
        return 0
      fi
    fi
    echo "  attempt $i/$HEALTH_ATTEMPTS — health=$status"
    i=$((i + 1))
    sleep 5
  done
  echo "ERROR: rolled-back stack did not become ready within $((HEALTH_ATTEMPTS * 5))s"
  docker logs --tail 150 "$APP_CONTAINER" 2>&1 || true
  return 1
}

main() {
  maybe_downgrade_migrations || { echo "Aborting rollback: migration downgrade guard refused."; return 1; }
  restore_previous_image
  health_check
}

# Only run when EXECUTED directly (not when sourced by a test).
case "${0##*/}" in
  rollback_stack.sh) main "$@" ;;
esac
