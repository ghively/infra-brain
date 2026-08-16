"""Tests for the require-worktree-isolation PreToolUse hook.

Exercises the hook's core decision logic directly via the importable `decide()`
function (and the small helpers) rather than shelling out, so the suite is fast,
deterministic, and has no external dependencies (no DB, no network).

Coverage:
  * the original cases the hook was designed around (code-writing verbs block,
    isolation=worktree allows, read-only subagent types allow, override token allows,
    non-code-writing dispatch allows, non-Agent tools allow);
  * bug 1 — broadened word-boundary verb detection (the 6+ audit false negatives now
    block) while genuine read-only phrasings that merely *contain* a verb substring
    (address/asset/reset/wired-in-prose) still allow;
  * bug 2 — malformed / wrong-typed input never raises and reaches a deliberate
    allow/block;
  * bug 3 — override token is word-boundary matched, so an embedded-substring bypass
    does NOT override and prose merely mentioning the token does NOT override.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

# --- load the hyphen-named hook module from its file path ---------------------------
_HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "hooks"
    / "require-worktree-isolation.py"
)
_spec = importlib.util.spec_from_file_location("require_worktree_isolation", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# --- helpers ------------------------------------------------------------------------
def agent_payload(prompt="", description="", **tool_input_extra):
    ti = {"prompt": prompt, "description": description}
    ti.update(tool_input_extra)
    return {"tool_name": "Agent", "tool_input": ti}


def code_exit(data):
    """Return just the exit code from decide()."""
    return hook.decide(data)[0]


ALLOW = 0
BLOCK = 2


# ============================================================================
# Original cases the hook was designed around
# ============================================================================
def test_non_agent_tool_allows():
    assert code_exit({"tool_name": "Bash", "tool_input": {}}) == ALLOW


def test_code_writing_dispatch_blocks_without_isolation():
    data = agent_payload(prompt="Implement the new retry decorator in resilience.py")
    assert code_exit(data) == BLOCK


def test_isolation_worktree_allows():
    data = agent_payload(
        prompt="Implement the new retry decorator", isolation="worktree"
    )
    assert code_exit(data) == ALLOW


@pytest.mark.parametrize("subagent", sorted(hook.READ_ONLY_SUBAGENTS))
def test_read_only_subagent_types_allow(subagent):
    # Even with a code-writing verb in the prompt, exempt types pass.
    data = agent_payload(
        prompt="Refactor and rewrite the module", subagent_type=subagent
    )
    assert code_exit(data) == ALLOW


def test_override_token_allows():
    data = agent_payload(
        prompt="Implement the parser. NO_WORKTREE_OK — sole dispatch this batch."
    )
    assert code_exit(data) == ALLOW


def test_non_code_writing_dispatch_allows():
    data = agent_payload(
        prompt="Research how the sweep graph topology is derived and report findings."
    )
    assert code_exit(data) == ALLOW


def test_fix_and_migration_phrasings_block():
    assert code_exit(agent_payload(prompt="fix(host_reconcile): guard the correlation")) == BLOCK
    assert code_exit(agent_payload(prompt="Generate an Alembic migration for the new column")) == BLOCK
    assert code_exit(agent_payload(prompt="Scaffold a new domain agent from the template")) == BLOCK


# ============================================================================
# Bug 1 — broadened, word-boundary verb detection (audit false negatives)
# ============================================================================
# Each of these describes obvious file-editing work but previously sailed through.
AUDIT_FALSE_NEGATIVES = [
    "Update the config.py default for the retry budget.",
    "Change the healthz probe path in k8s/agent-core.yaml.",
    "Replace the deprecated shutdown call in scheduler.py.",
    "Append a new field to the drift_events model.",
    "Insert a guard clause at the top of the collector loop.",
    "Set up the new FastAPI router for the metrics endpoint.",
    "Wire the new callback into callbacks/registry.py.",
    "Correct the off-by-one in the pagination helper.",
    "Adjust the timeout in the readonly HTTP client.",
    "Apply the DLP pattern fix across the tool files.",
    "Add a column to the resources table.",
    "Add the missing null check.",  # bare "add" not followed by a/the-file
]


@pytest.mark.parametrize("prompt", AUDIT_FALSE_NEGATIVES)
def test_audit_false_negatives_now_block(prompt):
    assert code_exit(agent_payload(prompt=prompt)) == BLOCK


# Genuine read-only phrasings that merely CONTAIN a verb as a substring must still
# allow — the word-boundary matching must not create false positives.
@pytest.mark.parametrize(
    "prompt",
    [
        "Investigate the IP address correlation logic and summarize it.",
        "Audit the asset inventory collector and report gaps.",
        "Analyze how the reset behavior of the circuit breaker works.",
        "Summarize the changelog entries for the last release.",
    ],
)
def test_read_only_lookalikes_still_allow(prompt):
    assert code_exit(agent_payload(prompt=prompt)) == ALLOW


def test_add_matches_only_as_whole_word():
    # substring inside "address" must not trip the verb matcher
    assert hook.looks_code_writing("the ip address is stale") is False
    # bare "add" as its own word must trip it
    assert hook.looks_code_writing("add the guard") is True


def test_set_matches_only_as_whole_phrase_or_word():
    assert hook.looks_code_writing("inspect the asset list") is False
    assert hook.looks_code_writing("reset the counter is fine") is False
    assert hook.looks_code_writing("set up the router") is True


# ============================================================================
# Bug 2 — malformed / wrong-typed input never raises
# ============================================================================
MALFORMED_PAYLOADS = [
    "a bare string, not an object",
    12345,
    ["a", "list"],
    None,
    True,
    {"tool_name": "Agent", "tool_input": "not-a-dict"},
    {"tool_name": "Agent", "tool_input": 42},
    {"tool_name": "Agent", "tool_input": ["x"]},
    {"tool_name": "Agent", "tool_input": None},
    {"tool_name": "Agent", "tool_input": {"prompt": None, "description": None}},
    {"tool_name": "Agent", "tool_input": {"prompt": 123, "description": 4.5}},
    {"tool_name": "Agent", "tool_input": {"prompt": ["implement", "this"]}},
    {"tool_name": "Agent", "tool_input": {"prompt": {"nested": "implement"}}},
    {"tool_name": "Agent", "tool_input": {"subagent_type": 99, "prompt": "implement x"}},
    {"tool_name": "Agent", "tool_input": {"isolation": ["worktree"], "prompt": "implement x"}},
    {},  # missing everything
]


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
def test_malformed_input_never_raises_and_returns_valid_code(payload):
    # Must not raise and must return a deliberate 0 or 2.
    code, _msg = hook.decide(payload)
    assert code in (ALLOW, BLOCK)


def test_wrong_typed_prompt_still_scanned_for_verbs():
    # A list/dict prompt is coerced to text; the verb inside is still detected → block.
    data = {"tool_name": "Agent", "tool_input": {"prompt": ["implement", "the thing"]}}
    assert code_exit(data) == BLOCK
    data2 = {"tool_name": "Agent", "tool_input": {"prompt": {"task": "refactor module"}}}
    assert code_exit(data2) == BLOCK


def test_non_string_subagent_type_does_not_crash_and_is_not_exempt():
    # subagent_type is a number → not a string → not treated as exempt; verb blocks.
    data = {"tool_name": "Agent", "tool_input": {"subagent_type": 7, "prompt": "implement x"}}
    assert code_exit(data) == BLOCK


def test_as_text_coercion():
    assert hook._as_text(None) == ""
    assert hook._as_text("hi") == "hi"
    assert hook._as_text(123) == "123"
    assert "implement" in hook._as_text(["implement"])


# ============================================================================
# Bug 3 — override token word-boundary matching
# ============================================================================
def test_embedded_substring_does_not_override():
    # "xNO_WORKTREE_OKx" contains the token as a substring but not as a word.
    data = agent_payload(prompt="Implement the parser. reno_worktree_okay xNO_WORKTREE_OKx")
    assert code_exit(data) == BLOCK


def test_override_token_glued_to_word_does_not_fire():
    data = agent_payload(prompt="Implement it FOONO_WORKTREE_OKBAR now")
    assert code_exit(data) == BLOCK


def test_prose_mentioning_token_still_overrides_because_bare_word_present():
    # NOTE: a bare, whole-word occurrence DOES override by design — even in prose like
    # "don't use NO_WORKTREE_OK here". The word-boundary fix's job is only to stop
    # substring bypasses, not to parse intent. Documented behavior, asserted explicitly.
    data = agent_payload(prompt="Implement it. Do not use NO_WORKTREE_OK for this one.")
    assert code_exit(data) == ALLOW


def test_override_token_as_bare_word_overrides():
    data = agent_payload(prompt="Refactor module. NO_WORKTREE_OK")
    assert code_exit(data) == ALLOW


def test_has_override_helper():
    assert hook.has_override("NO_WORKTREE_OK") is True
    assert hook.has_override("no_worktree_ok") is True  # case-insensitive
    assert hook.has_override("xNO_WORKTREE_OKx") is False
    assert hook.has_override("nothing here") is False


# ============================================================================
# main() entrypoint smoke test — stdin in, exit code out, never raises
# ============================================================================
def _run_main_with_stdin(payload_str):
    """Invoke main() with a fake stdin, capturing the SystemExit code."""
    old_stdin, old_stderr = sys.stdin, sys.stderr
    sys.stdin = io.StringIO(payload_str)
    sys.stderr = io.StringIO()
    try:
        with pytest.raises(SystemExit) as exc:
            hook.main()
        return exc.value.code
    finally:
        sys.stdin, sys.stderr = old_stdin, old_stderr


def test_main_blocks_code_writing():
    code = _run_main_with_stdin(json.dumps(agent_payload(prompt="implement the feature")))
    assert code == BLOCK


def test_main_allows_isolation():
    code = _run_main_with_stdin(
        json.dumps(agent_payload(prompt="implement the feature", isolation="worktree"))
    )
    assert code == ALLOW


def test_main_allows_unparseable_stdin():
    code = _run_main_with_stdin("{not valid json")
    assert code == ALLOW
