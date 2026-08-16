"""Runtime self-check (P3.1): the subset of .claude/scripts/dev_status.py's
10 checks that need only already-imported live objects or the source tree
already baked into the deployed image (both present in the running app/mcp
containers) — no git, no dev venv, no k8s manifests, no .env.example, none
of which the container filesystem has. Checks 2 (test coverage), 5/6
(migration/alembic-lock — need the repo's alembic/ history + a dev venv), 7
(k8s manifests — not copied into the image), 8 (env parity — needs
.env.example), and 9 (CLAUDE.md staleness — editorial, not a runtime fact)
stay dev-only in dev_status.py; they answer "is the checked-out repo in good
shape", not "is this running instance healthy", so a live endpoint can't
usefully answer them anyway.

Each check function's logic is a straight port of the corresponding
dev_status.py check — kept in sync manually (no shared import: dev_status.py
runs in .claude/scripts/, a Claude Code developer-tooling directory that's
never shipped, whereas this module ships with the app and needs to stay
importable with zero optional dependencies).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

Status = Literal["ok", "warn", "error"]

# Domains intentionally without a default cron schedule (on-demand, hook-only,
# or run via a scoped job — see scheduler.py's _DEFAULT_SCHEDULES comment).
_SCHEDULER_EXEMPT = {"integration", "drift", "notification", "inventory_mr", "query"}
_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
_ETL_BASE_PY = Path(__file__).resolve().parent / "etl" / "base.py"


def _result(name: str, status: Status, message: str) -> dict:
    return {"name": name, "status": status, "message": message}


def check_agent_registry() -> dict:
    from infra_brain.scheduler import _DEFAULT_SCHEDULES
    from infra_brain.supervisor import AGENT_REGISTRY, SKIP_HOOK

    # set(AGENT_REGISTRY) / set(SKIP_HOOK) drive _LazyAgentRegistry.__iter__(),
    # which resolves (imports) every domain. For any domain outside
    # supervisor._OPTIONAL_DOMAINS, a broken import is deliberately re-raised
    # uncaught (F-009: "must be LOUD"). That's the right behavior for most
    # callers, but this function backs the /selfcheck endpoint, whose entire
    # purpose is to report a graceful {"overall": "error", ...} payload
    # instead of a 500 — so a broken required agent module must surface as an
    # "error" check result here, not an unhandled exception.
    try:
        registry = set(AGENT_REGISTRY)
        skip_hook = set(SKIP_HOOK)
    except ImportError as exc:
        return _result(
            "agent_registry",
            "error",
            f"Agent module import failed: {exc}",
        )
    # Deliberately _DEFAULT_SCHEDULES only, NOT _ADHOC_SCHEDULES — the latter
    # carries non-agent housekeeping cron entries (collection_health,
    # retention_prune, sweep_graph, ...) that were never meant to appear in
    # AGENT_REGISTRY, so including them here would misreport them as
    # "unknown domains". check_schedule_collisions() below is where both
    # dicts legitimately combine (collision detection cares about cron
    # times, not domain membership).
    schedules = set(_DEFAULT_SCHEDULES)

    issues = []
    orphan_skip = skip_hook - registry
    if orphan_skip:
        issues.append(f"SKIP_HOOK has unknown domains: {sorted(orphan_skip)}")
    orphan_sched = schedules - registry
    if orphan_sched:
        issues.append(f"schedules has unknown domains: {sorted(orphan_sched)}")
    unscheduled = registry - schedules - _SCHEDULER_EXEMPT
    if unscheduled:
        issues.append(f"Unscheduled (not in SCHEDULER_EXEMPT): {sorted(unscheduled)}")

    if issues:
        return _result("agent_registry", "error", "; ".join(issues))
    return _result(
        "agent_registry",
        "ok",
        f"{len(registry)} agents, {len(schedules)} scheduled, {len(skip_hook)} skip-hook",
    )


def check_schedule_collisions() -> dict:
    from infra_brain.scheduler import _ADHOC_SCHEDULES, _DEFAULT_SCHEDULES

    pairs = list(_DEFAULT_SCHEDULES.items()) + list(_ADHOC_SCHEDULES.items())
    by_time: dict[tuple[str, str], list[str]] = {}
    for domain, cron in pairs:
        parts = cron.split()
        if len(parts) >= 2:
            by_time.setdefault((parts[1], parts[0]), []).append(domain)  # (hour, minute)

    collisions = {k: v for k, v in by_time.items() if len(v) > 1}
    if collisions:
        lines = [f"hour={h} min={m}: {sorted(d)}" for (h, m), d in sorted(collisions.items())]
        return _result(
            "schedule_collisions",
            "warn",
            f"{len(collisions)} time slot(s) with multiple agents: " + "; ".join(lines),
        )
    return _result("schedule_collisions", "ok", f"No collisions across {len(pairs)} domains")


def check_callbacks_wiring() -> dict:
    name = "callbacks_wiring"
    if _ETL_BASE_PY.exists() and "build_callbacks" not in _ETL_BASE_PY.read_text(
        encoding="utf-8"
    ):
        return _result(
            name,
            "error",
            "etl/base.py (ETLConnector) does not reference build_callbacks — ALL agents unwired",
        )

    exempt = {"__init__", "base", "llm_base"}
    wired_patterns = ("ETLConnector", "BaseAgent", "LLMAgent", "build_callbacks")
    unwired = []
    for f in sorted(_AGENTS_DIR.glob("*.py")):
        if f.stem in exempt or f.stem.startswith("_"):
            continue
        src = f.read_text(encoding="utf-8")
        if not re.search(r"^class ", src, re.MULTILINE):
            continue
        if not any(p in src for p in wired_patterns):
            unwired.append(f.stem)
    if unwired:
        return _result(
            name,
            "error",
            f"Agent(s) neither inherit BaseAgent nor import build_callbacks: {unwired}",
        )
    return _result(name, "ok", "All agents inherit BaseAgent (which wires build_callbacks)")


def run_selfcheck() -> dict:
    """Run every runtime-viable check and roll up an overall status."""
    checks = [check_agent_registry(), check_schedule_collisions(), check_callbacks_wiring()]
    statuses = {c["status"] for c in checks}
    overall: Status = "error" if "error" in statuses else "warn" if "warn" in statuses else "ok"
    return {"overall": overall, "checks": checks}
