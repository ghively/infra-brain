#!/usr/bin/env python3
"""UserPromptSubmit hook: make orchestration the default MODE for this repo.

Injects a routing reminder on every user prompt so substantive work goes through the
`orchestrator` SKILL (decompose, then dispatch specialists in parallel yourself, from
this session — see .claude/skills/orchestrator/SKILL.md), while simple questions /
single-file reads are still handled directly.

As of 2026-07-22 there is no spawnable `orchestrator` subagent — subagents have no
Agent/Task tool available to themselves (confirmed empirically), so
`Agent(subagent_type="orchestrator")` cannot do the fan-out job it used to claim to.
See docs/decisions/2026-07-22-orchestrator-redesign-plan.md.

This is a NUDGE, not a block — exit 0 always. The reminder text itself carries the
SIMPLE-task carve-outs so the main agent self-filters.
"""
import json
import sys

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

prompt = (data.get("prompt") or "").lower()

# Best-effort: never inject inside a subagent context. UserPromptSubmit normally fires only
# at the top level, but if any field signals a nested/subagent invocation, stay silent so the
# orchestrator (and other subagents) are never told to spawn an orchestrator. This is the
# mechanism that prevents the orchestrator→orchestrator recursion at the hook layer.
def _truthy(*keys):
    return any(data.get(k) for k in keys)

if _truthy("subagent_type", "is_subagent", "parent_session_id", "parent_tool_use_id", "agent_name"):
    sys.exit(0)

# Don't nag if the user is already steering routing explicitly, or is clearly mid-task.
_skip_markers = ("orchestrat", "don't orchestrat", "do not orchestrat", "no orchestrat",
                 "work directly", "skip the orchestrator")
if any(m in prompt for m in _skip_markers):
    sys.exit(0)

# F-016: inject only on prompts that look genuinely substantive/multi-step.
# Short prompts with no work verb are questions/status checks — stay silent.
_SUBSTANTIVE_MARKERS = (
    "build", "implement", "fix", "debug", "deploy", "migrat", "refactor",
    "add ", "create", "wire", "sweep", "schema", "review", "test", "agent",
)
if len(prompt) < 80 and not any(m in prompt for m in _SUBSTANTIVE_MARKERS):
    sys.exit(0)

reminder = (
    "infra-brain routing: substantive/multi-step work goes through orchestration MODE — "
    "invoke the Skill(skill=\"orchestrator\") skill to decompose, then dispatch "
    "specialist Agent() calls yourself, in parallel batches, with isolation:\"worktree\" "
    "on anything code-writing (a hook enforces this). Skip for simple questions/"
    "single-file reads.\n"
    "There is no orchestrator SUBAGENT anymore — never call "
    'Agent(subagent_type="orchestrator"); that type does not exist. If you are ALREADY '
    "a subagent, you have no Agent/Task tool at all — do your assigned task directly "
    "and report results upward instead of trying to dispatch further."
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": reminder,
    }
}))
sys.exit(0)
