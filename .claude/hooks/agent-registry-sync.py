"""PostToolUse: validate AGENT_REGISTRY ↔ SKIP_HOOK ↔ _DEFAULT_SCHEDULES consistency.

Runs when supervisor.py or scheduler.py is edited. Catches the three most common
bugs when adding a new agent: forgetting SKIP_HOOK, forgetting a default schedule,
or registering a domain in the scheduler that supervisor doesn't know about.

None of those three structures is a hand-maintained literal any more: supervisor.py
builds AGENT_REGISTRY/SKIP_HOOK from _AGENT_SPECS (module-path/class-name strings)
plus each agent class's declarative AgentSpec, and scheduler.py's _DEFAULT_SCHEDULES
is a comprehension over the registry. Regexing the source for the old
`"domain": SomeAgent` / `SKIP_HOOK = {...}` / `_DEFAULT_SCHEDULES = {...}` literals
matched nothing, so every assertion below passed vacuously (the false "0 agents"
drift — same breakage .claude/scripts/dev_status.py documents around its
"Registry introspection" section).

We read the same declarative source of truth dev_status.py introspects, but via ast
rather than by importing it: a PostToolUse hook has no WARN state to degrade to, so
an unavailable venv would put it straight back into the silent no-op it is being
fixed out of — and it fires on every edit, where importing all ~30 agent modules is
exactly the cost the lazy registry exists to avoid. Reading _AGENT_SPECS and each
class's AgentSpec keywords is not the regex-a-literal pattern that broke.
"""
import ast
import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
file_path = payload.get("tool_input", {}).get("file_path", "")

if tool not in ("Edit", "Write"):
    sys.exit(0)

relevant = {"supervisor.py", "scheduler.py"}
if not any(r in file_path for r in relevant):
    sys.exit(0)

# This file lives at .claude/hooks/ — derive the root from it, not from the edited
# file's path (which may be relative, and whose depth varies).
project_root = Path(__file__).resolve().parents[2]
supervisor_py = project_root / "src" / "infra_brain" / "supervisor.py"
scheduler_py = project_root / "src" / "infra_brain" / "scheduler.py"

if not supervisor_py.exists() or not scheduler_py.exists():
    sys.exit(0)

sup_src = supervisor_py.read_text(encoding="utf-8")
sch_src = scheduler_py.read_text(encoding="utf-8")


def _assigned_value(tree: ast.Module, name: str) -> ast.expr | None:
    """Return the value node of a module-level `name = ...` / `name: T = ...`."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value
    return None


def extract_agent_specs(src: str) -> dict[str, tuple[str, str]]:
    """{domain: (module path, class name)} from supervisor.py's _AGENT_SPECS."""
    value = _assigned_value(ast.parse(src), "_AGENT_SPECS")
    if value is None:
        return {}
    try:
        return ast.literal_eval(value)
    except ValueError:
        return {}


def extract_class_meta(module_path: str, class_name: str) -> dict | None:
    """Return {"schedule": ..., "skip_hook": ...} for an agent class, or None."""
    path = project_root / "src" / Path(*module_path.split(".")).with_suffix(".py")
    if not path.exists():
        return None
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        meta = {"schedule": None, "skip_hook": False}
        spec_kwargs: dict = {}
        for stmt in node.body:
            if not (isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)):
                continue
            target = stmt.targets[0].id
            try:
                if target in meta:
                    meta[target] = ast.literal_eval(stmt.value)
                elif target == "spec" and isinstance(stmt.value, ast.Call):
                    spec_kwargs = {
                        kw.arg: ast.literal_eval(kw.value)
                        for kw in stmt.value.keywords
                        if kw.arg in meta
                    }
            except ValueError:
                continue
        # A class body declaring both `spec` and a direct attr: spec wins
        # (etl/base.py's __init_subclass__ overwrites the direct attr).
        meta.update(spec_kwargs)
        return meta
    return None


specs = extract_agent_specs(sup_src)

registry: set[str] = set(specs)
skip_hook: set[str] = set()
schedules: set[str] = set()
unreadable: list[str] = []

for domain, (module_path, class_name) in specs.items():
    meta = extract_class_meta(module_path, class_name)
    if meta is None:
        unreadable.append(f"{domain} ({module_path}.{class_name})")
        continue
    if meta["skip_hook"]:
        skip_hook.add(domain)
    if meta["schedule"] is not None:
        schedules.add(domain)

# Domains intentionally without a default cron schedule (on-demand, hook-only, or
# run via a scoped job) — same list dev_status.py::check_agent_registry uses.
SCHEDULER_EXEMPT = {"integration", "drift", "notification", "inventory_mr", "query"}

issues = []

# An empty registry means the shapes read above moved again — fail loudly rather
# than pass every assertion below vacuously (the exact bug this hook had).
if not registry:
    issues.append(
        "could not read _AGENT_SPECS from supervisor.py — this hook's model of the "
        "registry is stale and every check below would pass vacuously"
    )

if unreadable:
    issues.append(f"agent class not found for registered domains: {sorted(unreadable)}")

# _DEFAULT_SCHEDULES must stay derived from AGENT_REGISTRY; a hand-maintained
# literal would reintroduce the parallel source of truth this hook assumes is gone.
_sched_value = _assigned_value(ast.parse(sch_src), "_DEFAULT_SCHEDULES")
if _sched_value is None or not any(
    isinstance(n, ast.Name) and n.id == "AGENT_REGISTRY" for n in ast.walk(_sched_value)
):
    issues.append(
        "scheduler.py's _DEFAULT_SCHEDULES no longer derives from AGENT_REGISTRY — "
        "the agent class's declarative schedule must stay the single source of truth"
    )

# SKIP_HOOK must only contain domains that are in AGENT_REGISTRY
orphan_skip = skip_hook - registry
if orphan_skip:
    issues.append(f"SKIP_HOOK contains domains not in AGENT_REGISTRY: {sorted(orphan_skip)}")

# Every scheduled domain must be in AGENT_REGISTRY
orphan_sched = schedules - registry
if orphan_sched:
    issues.append(f"_DEFAULT_SCHEDULES contains domains not in AGENT_REGISTRY: {sorted(orphan_sched)}")

# Every AGENT_REGISTRY domain (except exempt) should have a schedule
unscheduled = registry - schedules - SCHEDULER_EXEMPT
if unscheduled:
    issues.append(
        f"AGENT_REGISTRY domains have no default schedule (add a schedule to the "
        f"agent's AgentSpec or to SCHEDULER_EXEMPT): {sorted(unscheduled)}"
    )

if issues:
    print("agent-registry-sync: REGISTRY CONSISTENCY ISSUES DETECTED")
    for issue in issues:
        print(f"  ✗ {issue}")
    print()
    print("Fix: keep _AGENT_SPECS (supervisor.py) and each agent class's AgentSpec "
          "(schedule/skip_hook) in sync — see /agent-register.")
    sys.exit(1)

print(f"agent-registry-sync: OK — {len(registry)} agents, {len(schedules)} scheduled, {len(skip_hook)} skip-hook")
