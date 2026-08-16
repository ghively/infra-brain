#!/bin/sh
# scripts/deploy/downgrade_guard.sh
#
# PURE decision logic for the guarded, opt-in Alembic migration downgrade that
# runs during a rollback (TRK-005). It performs NO I/O — no Docker, no DB, no
# alembic — so it is deterministic and unit-testable (see
# tests/deploy/test_downgrade_guard.py). rollback_stack.sh sources this file and
# feeds it facts it gathered from the live system; this file only DECIDES.
#
# POSIX sh on purpose: the GitLab deploy/rollback jobs run on the docker:24
# image (Alpine/busybox), which has NO bash. Keep this bash-free.
#
# Downgrading migrations can DESTROY DATA (a destructive downgrade drops tables/
# columns). So the default posture is DO NOTHING; every step to actually run a
# downgrade must be explicitly opted into. decide_downgrade() encodes exactly
# that ladder and prints ONE decision token on stdout:
#
#   SKIP_OPTOUT            downgrade not opted in (ALLOW_MIGRATION_DOWNGRADE!=1)
#   SKIP_NO_TARGET         no captured pre-deploy revision to downgrade TO
#   SKIP_NOOP             live DB is already at the captured revision
#   REFUSE_UNKNOWN_CURRENT could not determine the live DB revision — refuse
#   REFUSE_DESTRUCTIVE     downgrade path drops data and that was not allowed
#   DOWNGRADE              safe to run `alembic downgrade <target>`
#
# Arguments (all positional, all strings):
#   $1 current              live DB alembic revision ("" if unknown)
#   $2 target               captured pre-deploy revision to downgrade to ("" if none)
#   $3 allow_downgrade      "1" to opt into downgrading at all
#   $4 destructive          "1" if the downgrade path was found to drop data
#                           (or could NOT be verified — treated as destructive)
#   $5 allow_destructive    "1" to accept data loss and downgrade anyway
decide_downgrade() {
  current="$1"
  target="$2"
  allow_downgrade="$3"
  destructive="$4"
  allow_destructive="$5"

  # (1) Opt-in gate — the whole feature is off unless explicitly armed.
  if [ "$allow_downgrade" != "1" ]; then
    echo "SKIP_OPTOUT"
    return 0
  fi
  # (2) Nothing to downgrade to (e.g. first-ever deploy captured no revision).
  if [ -z "$target" ]; then
    echo "SKIP_NO_TARGET"
    return 0
  fi
  # (3) Already where we want to be — a downgrade would be a no-op (or an error).
  if [ -n "$current" ] && [ "$current" = "$target" ]; then
    echo "SKIP_NOOP"
    return 0
  fi
  # (4) We must know the current revision to safely compute/execute a downgrade.
  if [ -z "$current" ]; then
    echo "REFUSE_UNKNOWN_CURRENT"
    return 0
  fi
  # (5) Data-loss guard — a destructive path needs a SECOND explicit opt-in.
  if [ "$destructive" = "1" ] && [ "$allow_destructive" != "1" ]; then
    echo "REFUSE_DESTRUCTIVE"
    return 0
  fi
  # (6) All guards cleared — safe to downgrade.
  echo "DOWNGRADE"
  return 0
}

# scan_destructive_sql — read Alembic offline-downgrade SQL on stdin and print a
# single "1" (destructive: drops data) or "0" (non-destructive) on stdout. This
# is a PURE text scan (no Docker, no DB, no alembic), extracted here so the
# destructive verdict rollback_stack.sh feeds into decide_downgrade() is itself
# unit-testable (see tests/deploy/test_downgrade_guard.py).
#
# Conservative by design: ANY table/column drop counts as destructive. Over-
# flagging is the SAFE direction — it forces a human decision (a second explicit
# opt-in) instead of silently dropping data during an automated rollback.
#
# HARD LIMITATION (CRITICAL #2, accepted / human-owned): this only ever sees the
# OFFLINE `alembic downgrade --sql` render. A downgrade() that hides data-
# mutating logic behind `context.is_offline_mode()` (or `if not is_offline_mode()`)
# will emit a CLEAN offline preview here yet DESTROY data on the real online run —
# silently defeating the destructive opt-in. This scan cannot detect that; it is
# NOT a substitute for reviewing downgrade() bodies.
#   MIGRATION-AUTHORING RULE: a migration's downgrade() MUST NOT place data-
#   mutating or destructive logic behind is_offline_mode() (offline and online
#   downgrade paths must be equivalent in what data they touch), or this guard's
#   preview is a lie.
#
# ACCEPTED GAPS (MEDIUM #4/#5, human-owned): this is a line-based regex and only
# flags DROP TABLE / DROP COLUMN. It does NOT flag other data-loss DDL/DML:
# TRUNCATE, ALTER COLUMN ... TYPE narrowing (lossy casts), DROP TYPE / ALTER TYPE
# ... DROP VALUE (enums), or bulk DELETE / UPDATE. Treat a "0" as "no OBVIOUS
# table/column drop", not a proof of safety — always review the downgrade() and,
# for anything risky, use the destructive opt-in deliberately.
scan_destructive_sql() {
  if grep -Eiq 'DROP[[:space:]]+TABLE|DROP[[:space:]]+COLUMN|ALTER[[:space:]]+TABLE[[:space:]].*DROP[[:space:]]+COLUMN'; then
    echo 1
  else
    echo 0
  fi
}

# CLI entrypoint: only run when this file is EXECUTED directly (so sourcing it
# from rollback_stack.sh, or from a test that sources it, does not run anything).
# POSIX-safe self-vs-source check: when executed, $0 basename is this script;
# when sourced, $0 is the caller's shell name.
case "${0##*/}" in
  downgrade_guard.sh) decide_downgrade "$@" ;;
esac
