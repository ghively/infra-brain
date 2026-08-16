#!/usr/bin/env python3
"""PreToolUse hook: require isolation:"worktree" for code-writing Agent dispatches.

Part of the 2026-07-22 orchestrator redesign (see
docs/decisions/2026-07-22-orchestrator-redesign-plan.md, section 2.1). Enforces
mechanically what was previously only a documented convention — a worktree-isolation
gotcha had already been written down once before it regressed the same day, which is
exactly the failure mode this hook exists to close: three background subagents shared
one working tree, causing branch hijacking (one agent's `git checkout -b` silently
moved another's in-flight commits onto the wrong branch) and file cross-contamination
(a `git add <path>` swept a concurrent task's uncommitted hunks into the wrong commit).

Read-only subagent types (research/review/analysis — never write git-tracked files)
are exempt. Everything else defaults to REQUIRED when the dispatch looks code-writing
by a verb heuristic; pass NO_WORKTREE_OK anywhere in the prompt to deliberately opt out
for a single dispatch (e.g., the sole code-writing agent in a batch, nothing else
running concurrently).

This is a best-effort heuristic, not a proof: it fails OPEN (allows) when the dispatch
doesn't look code-writing by the verb check, since a false negative here just means
"no isolation enforced" (today's status quo), not something worse. A false positive
(blocking a genuinely read-only dispatch) is mitigated by the exemption list and the
override token.

Design notes for the three hardening fixes (2026-07-22 adversarial-audit follow-up):

  1. Verb detection is tokenized and word-boundary aware (regex over the lowercased
     text), not substring `in` checks, so "add"/"set"/"wire" match as whole words
     without matching "address"/"asset"/"wired-in-prose" false positives, and the
     list covers the common code-writing verbs the audit slipped past (update, change,
     replace, append, insert, set up, wire, correct, adjust, apply, generate, add, …).

  2. Every field access is type-guarded. `tool_input` may not be a dict; prompt /
     description / subagent_type may be None, numbers, lists, or nested dicts. All are
     coerced safely and the hook NEVER raises — it always reaches a deliberate
     exit(0)/exit(2). A top-level guard in main() converts any unexpected error into a
     fail-open allow with a diagnostic on stderr (a crashing hook is not fail-closed).

  3. The override token is matched with a word-boundary regex (`\bNO_WORKTREE_OK\b`),
     not a bare substring, so neither an embedded-substring bypass
     (`xNO_WORKTREE_OKx`) nor prose merely mentioning the token fires the override
     unless the bare token appears as its own word.

Exit codes: 0 = allow, 2 = block (stderr shown to the model).
"""
import json
import re
import sys

READ_ONLY_SUBAGENTS = {
    "infra-researcher",
    "lc-safety-reviewer",
    "lc-migration-reviewer",
    "lc-api-reviewer",
    "lc-agent-completeness",
    "sweep-health",
    "drift-analyst",
    "Explore",
    "Plan",
}

# Code-writing verbs / phrases. Matched as whole tokens (word-boundary regex) against
# the lowercased prompt+description. Multi-word entries ("set up", "write code") match
# as adjacent-word phrases. Keep low false-negative for real editing dispatches while
# avoiding matching substrings inside unrelated words (e.g. "add" must not match
# "address", "set" must not match "asset/reset", "wire" must not match "wired" prose —
# handled by \b anchoring on both ends).
_WRITE_VERBS = (
    # core edit/create verbs
    "implement",
    "fix",
    "edit",
    "commit",
    "refactor",
    "build",
    "modify",
    "patch",
    "rewrite",
    "scaffold",
    "create",
    "delete",
    "remove",
    # verbs the adversarial audit slipped past
    "update",
    "change",
    "replace",
    "append",
    "insert",
    "wire",
    "correct",
    "adjust",
    "apply",
    "generate",
    "add",
    "set up",
    "write",
    # schema / migration
    "migration",
    "migrate",
)

# Precompile one alternation regex. Sort longest-first so multi-word phrases win, and
# escape each term. `\b` on both ends gives whole-word / whole-phrase matching; for a
# phrase like "set up" the internal space is matched as a flexible run of whitespace
# and `\b` anchors the outer edges.
_VERB_ALTERNATION = "|".join(
    re.escape(v).replace(r"\ ", r"\s+")
    for v in sorted(_WRITE_VERBS, key=len, reverse=True)
)
_WRITE_VERB_RE = re.compile(rf"\b(?:{_VERB_ALTERNATION})\b", re.IGNORECASE)

_OVERRIDE_RE = re.compile(r"\bNO_WORKTREE_OK\b", re.IGNORECASE)

BLOCK_MESSAGE = (
    'BLOCKED (worktree-isolation policy, 2026-07-22 orchestrator redesign): this '
    'looks like a code-writing subagent dispatch without isolation:"worktree". '
    "Shared-working-tree dispatches caused real branch-hijacking and file "
    "cross-contamination between concurrent tasks earlier today (see "
    "docs/decisions/2026-07-22-orchestrator-redesign-plan.md sec 2.1) -- and it was "
    "a REPEAT of an already-documented incident. Add isolation:\"worktree\" to this "
    "Agent call, or include NO_WORKTREE_OK in the prompt if you've deliberately "
    "decided a shared tree is safe here (e.g. the only code-writing dispatch in "
    "this batch, nothing else running concurrently)."
)


def _as_text(value):
    """Coerce an arbitrary JSON value to a string safely.

    Strings pass through; None becomes "". Everything else (numbers, lists, dicts,
    bools) is stringified so a wrong-typed field can neither crash the hook nor
    silently vanish — its textual form is still scanned for verbs / the override
    token. Never raises.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def looks_code_writing(text):
    """True if the text contains a code-writing verb as a whole word/phrase."""
    return bool(_WRITE_VERB_RE.search(text))


def has_override(text):
    """True only if the bare NO_WORKTREE_OK token appears as its own word."""
    return bool(_OVERRIDE_RE.search(text))


def decide(data):
    """Pure decision function. Returns (exit_code, message).

    exit_code 0 = allow (message is a short reason, not shown to the model),
    exit_code 2 = block (message is shown to the model on stderr).

    Accepts any JSON-decoded payload. Never raises for malformed input — every
    field access is type-guarded and wrong-typed values are coerced to text.
    """
    if not isinstance(data, dict):
        return 0, "allow: payload is not an object"

    if data.get("tool_name") not in ("Agent", "Task"):
        return 0, "allow: not an Agent/Task dispatch"

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        # No structured input we can inspect — nothing to enforce against.
        return 0, "allow: tool_input is not an object"

    # Explicit isolation already requested — always allow.
    if tool_input.get("isolation") == "worktree":
        return 0, "allow: isolation=worktree present"

    subagent_type = tool_input.get("subagent_type")
    if isinstance(subagent_type, str) and subagent_type in READ_ONLY_SUBAGENTS:
        return 0, "allow: read-only subagent type"

    text = _as_text(tool_input.get("prompt")) + " " + _as_text(tool_input.get("description"))

    if has_override(text):
        return 0, "allow: NO_WORKTREE_OK override present"

    if not looks_code_writing(text):
        return 0, "allow: does not look code-writing"

    return 2, BLOCK_MESSAGE


def main():
    try:
        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            # Unparseable stdin — nothing to enforce against; fail open.
            sys.exit(0)

        exit_code, message = decide(data)
        if exit_code != 0:
            print(message, file=sys.stderr)
        sys.exit(exit_code)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never crash the hook
        # A crashing hook is an accidental fail-open with a confusing traceback surfaced
        # to the model. Make the fail-open deliberate and diagnosable instead.
        print(
            f"worktree-isolation hook: internal error, allowing dispatch ({exc!r})",
            file=sys.stderr,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
