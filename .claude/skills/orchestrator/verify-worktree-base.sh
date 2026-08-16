#!/usr/bin/env bash
# verify-worktree-base: confirm a worktree-isolated Agent dispatch's branch actually
# descends from the BASE it was supposed to be dispatched from, before folding it.
#
# Why this exists: on 2026-07-22, isolation:"worktree" empirically branched 3 of 4
# identically-dispatched code-writing agents from stale `master` instead of the
# dispatching session's actual current branch tip. One agent noticed its own
# mis-basing and self-corrected; the other 3 didn't, and their branches silently
# reverted every file outside their own edits. SKILL.md Step 6.1 already documents
# this exact check as step 1 of the per-completing-agent procedure — this script
# just makes running it a single command instead of three, so there's no excuse to
# skip it under time pressure.
#
#   bash .claude/skills/orchestrator/verify-worktree-base.sh <BASE> <branch> [<branch> ...]
#
# Exit 0 = every branch verified sane (descends from BASE, non-empty commit range).
# Exit 1 = at least one branch failed verification — DO NOT fold it as-is; see the
#          printed diagnosis and either extract its changes surgically (cherry-pick /
#          `git show <branch>:<path>` per file) or re-dispatch it.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <BASE> <branch> [<branch> ...]" >&2
  exit 2
fi

BASE="$1"
shift

git rev-parse --verify --quiet "$BASE" >/dev/null || {
  echo "ERROR: BASE '$BASE' does not resolve to a commit in this repo" >&2
  exit 2
}

fail=0

for branch in "$@"; do
  git rev-parse --verify --quiet "$branch" >/dev/null || {
    echo "FAIL  $branch — does not exist"
    fail=1
    continue
  }

  if ! git merge-base --is-ancestor "$BASE" "$branch" 2>/dev/null; then
    fail=1
    actual_base="$(git merge-base "$BASE" "$branch" 2>/dev/null || echo "(no common ancestor found)")"
    echo "FAIL  $branch — does NOT descend from BASE ($BASE)"
    echo "      actual common ancestor: $actual_base"
    echo "      likely cause: the worktree was created from the wrong ref (e.g. stale"
    echo "      master) rather than BASE. Files this branch touches that also exist at"
    echo "      BASE but were NOT edited on this branch will silently revert to the"
    echo "      wrong-base version if merged wholesale."
    echo "      DO NOT merge this branch as-is. Extract this agent's intended changes"
    echo "      surgically instead, e.g.:"
    echo "        git show $branch:<path/to/file> > <path/to/file>   # per touched file"
    echo "      then commit those files onto the real integration branch yourself."
    echo ""
    continue
  fi

  n_commits="$(git rev-list --count "$BASE..$branch")"
  if [ "$n_commits" -eq 0 ]; then
    echo "FAIL  $branch — descends from BASE but has zero new commits (nothing to fold)"
    fail=1
    continue
  fi

  echo "PASS  $branch — descends from BASE, $n_commits new commit(s):"
  git log --oneline "$BASE..$branch" | sed 's/^/        /'
  echo "      files touched:"
  git diff --name-only "$BASE..$branch" | sed 's/^/        /'
  echo ""
done

exit "$fail"
