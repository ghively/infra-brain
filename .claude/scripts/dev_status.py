#!/usr/bin/env python3
"""dev_status.py — infra-brain developer tooling health audit.

Runs 10 validation checks without Docker or a live database.
Exit codes: 0 = all green, 1 = warnings only, 2 = at least one error.

Usage:
    python .claude/scripts/dev_status.py
    python .claude/scripts/dev_status.py --fix   # auto-update CLAUDE.md counts
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import subprocess
import sys
from pathlib import Path

# Ensure Unicode output works on Windows terminals (cp1252 → UTF-8).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------
_NO_COLOR = not sys.stdout.isatty() or bool(os.environ.get("NO_COLOR"))


def _green(s: str) -> str:
    return s if _NO_COLOR else f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return s if _NO_COLOR else f"\033[33m{s}\033[0m"


def _red(s: str) -> str:
    return s if _NO_COLOR else f"\033[31m{s}\033[0m"


def _bold(s: str) -> str:
    return s if _NO_COLOR else f"\033[1m{s}\033[0m"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
CheckResult = collections.namedtuple("CheckResult", ["name", "status", "message", "fix_hint"])


def ok(name: str, message: str) -> CheckResult:
    return CheckResult(name, "OK", message, "")


def warn(name: str, message: str, fix_hint: str = "") -> CheckResult:
    return CheckResult(name, "WARN", message, fix_hint)


def error(name: str, message: str, fix_hint: str = "") -> CheckResult:
    return CheckResult(name, "ERROR", message, fix_hint)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _find_python() -> str:
    for candidate in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = PROJECT_ROOT / candidate
        if p.exists():
            # Do NOT .resolve() — that follows the venv symlink to the base
            # interpreter and defeats venv site-packages detection (the venv
            # is identified by the invoked path, not the resolved target).
            return str(p)
    return sys.executable


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except FileNotFoundError as exc:
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# Registry introspection
#
# The agent registry, SKIP_HOOK, and schedules are no longer hand-maintained
# literals — they are derived at import time from the _AGENT_SPECS registry
# (supervisor.py) and each agent class's declarative `schedule`/`skip_hook`
# attributes (scheduler.py's _DEFAULT_SCHEDULES comprehension). Regexing the
# source for the old `"domain": SomeAgent` / `SKIP_HOOK = {...}` /
# `_DEFAULT_SCHEDULES = {...}` literals now silently matches nothing (the
# false "0 agents" drift). Introspect the real objects instead via the project
# venv (no DB connection — a dummy POSTGRES_URL only satisfies Settings
# construction, which validates non-empty but never connects).
# ---------------------------------------------------------------------------
_INTROSPECT_SNIPPET = (
    "import os, json;"
    "os.environ.setdefault('POSTGRES_URL','postgresql://audit:audit@localhost:5432/audit');"
    "from infra_brain.supervisor import AGENT_REGISTRY, SKIP_HOOK;"
    "from infra_brain.scheduler import _DEFAULT_SCHEDULES, _ADHOC_SCHEDULES;"
    "from infra_brain.etl.spec import retired_domains;"
    "print(json.dumps({"
    "'registry': sorted(AGENT_REGISTRY),"
    "'skip_hook': sorted(SKIP_HOOK),"
    "'schedules': dict(_DEFAULT_SCHEDULES),"
    "'retired': sorted(retired_domains()),"
    "'adhoc_schedules': dict(_ADHOC_SCHEDULES)}))"
)

_introspect_cache: dict | None = None


def _introspect() -> dict | None:
    """Return {'registry': [...], 'skip_hook': [...], 'schedules': {domain: cron}}
    by importing the real registry, or None if the package can't be imported
    (callers degrade to WARN). Cached for the run."""
    global _introspect_cache
    if _introspect_cache is not None:
        return _introspect_cache or None
    import json as _json

    rc, stdout, stderr = _run([_find_python(), "-c", _INTROSPECT_SNIPPET], cwd=str(PROJECT_ROOT))
    if rc != 0:
        _introspect_cache = {}
        return None
    try:
        _introspect_cache = _json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        _introspect_cache = {}
        return None
    return _introspect_cache


# ---------------------------------------------------------------------------
# Project root (this file lives at .claude/scripts/dev_status.py)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Check 1: Agent Registry Consistency
# ---------------------------------------------------------------------------
def check_agent_registry() -> CheckResult:
    name = "Agent Registry Consistency"
    data = _introspect()
    if data is None:
        return warn(
            name,
            "Could not import the registry (venv/deps unavailable) — skipped",
            fix_hint="Ensure .venv is present: uv sync",
        )

    registry = set(data["registry"])
    skip_hook = set(data["skip_hook"])
    schedules = set(data["schedules"])

    # Domains intentionally without a default cron schedule (on-demand, hook-only,
    # or run via a scoped job — see scheduler.py's _DEFAULT_SCHEDULES comment:
    # drift/notification/inventory_mr have schedule=None; query runs scoped).
    SCHEDULER_EXEMPT = {"integration", "drift", "notification", "inventory_mr", "query"}

    issues = []
    # Since skip_hook/schedules are derived FROM the registry at import time,
    # orphans are structurally impossible — but assert it anyway to catch a
    # future refactor that reintroduces a parallel source of truth.
    orphan_skip = skip_hook - registry
    if orphan_skip:
        issues.append(f"SKIP_HOOK has unknown domains: {sorted(orphan_skip)}")
    orphan_sched = schedules - registry
    if orphan_sched:
        issues.append(f"schedules has unknown domains: {sorted(orphan_sched)}")
    unscheduled = registry - schedules - SCHEDULER_EXEMPT
    if unscheduled:
        issues.append(f"Unscheduled (not in SCHEDULER_EXEMPT): {sorted(unscheduled)}")

    if issues:
        return error(
            name,
            "; ".join(issues),
            fix_hint="Run /agent-register or check each agent's AgentSpec (schedule/skip_hook)",
        )
    return ok(
        name, f"{len(registry)} agents, {len(schedules)} scheduled, {len(skip_hook)} skip-hook"
    )


# ---------------------------------------------------------------------------
# Check 2: Test File Coverage
# ---------------------------------------------------------------------------
def check_test_coverage() -> CheckResult:
    name = "Test File Coverage"
    agents_dir = PROJECT_ROOT / "src" / "infra_brain" / "agents"
    tests_dir = PROJECT_ROOT / "tests" / "agents"
    EXEMPT = {"__init__", "base", "llm_base"}

    missing = [
        f.stem
        for f in sorted(agents_dir.glob("*.py"))
        if f.stem not in EXEMPT
        and not f.stem.startswith("_")
        and not (tests_dir / f"test_{f.stem}.py").exists()
    ]

    if missing:
        return warn(
            name,
            f"{len(missing)} agent(s) lack a dedicated test file: {missing}",
            fix_hint="Run /agent-scaffold <name> or verify test_remaining_agents.py covers them",
        )
    return ok(name, "All agent files have matching test files")


# ---------------------------------------------------------------------------
# Check 3: Schedule Collision Detection
# ---------------------------------------------------------------------------
def check_schedule_collisions() -> CheckResult:
    name = "Schedule Collision Detection"
    data = _introspect()
    if data is None:
        return warn(
            name,
            "Could not import the registry (venv/deps unavailable) — skipped",
            fix_hint="Ensure .venv is present: uv sync",
        )

    # Retired domains (AgentSpec.retired) keep their cron string — it is the
    # cadence they would resume on — but SchedulerService.start() registers no
    # job for them, so they cannot contend with anything. Counting them would
    # report a phantom collision and push a live domain off a free slot.
    retired = set(data.get("retired", []))
    pairs = [
        (d, c)
        for d, c in list(data["schedules"].items()) + list(data.get("adhoc_schedules", {}).items())
        if d.split(":", 1)[0] not in retired
    ]
    by_time: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for domain, cron in pairs:
        parts = cron.split()
        if len(parts) >= 2:
            by_time[(parts[1], parts[0])].append(domain)  # (hour, minute)

    collisions = {k: v for k, v in by_time.items() if len(v) > 1}
    if collisions:
        lines = [
            f"  hour={h} min={m}: {sorted(domains)}"
            for (h, m), domains in sorted(collisions.items())
        ]
        return warn(
            name,
            f"{len(collisions)} time slot(s) with multiple agents:\n" + "\n".join(lines),
            fix_hint=(
                "Stagger by >=1 minute — each domain has its own dedup lock "
                "(keyed domain:scope, see dedup.py::_key), so a collision never "
                "silently drops a dispatch; the real cost is simultaneous "
                "correlated load on shared collection targets/DB."
            ),
        )
    return ok(name, f"No collisions across {len(pairs)} scheduled domains")


# ---------------------------------------------------------------------------
# Check 4: Callbacks Wiring
# ---------------------------------------------------------------------------
def check_callbacks_wiring() -> CheckResult:
    name = "Callbacks Wiring"
    agents_dir = PROJECT_ROOT / "src" / "infra_brain" / "agents"
    # build_callbacks is wired in ETLConnector.__init__ (etl/base.py) since the
    # god-file decomposition — BaseAgent/LLMAgent (agents/base.py, llm_base.py)
    # and every domain agent inherit it. The old check looked at agents/base.py
    # (false "ALL agents are unwired").
    base_py = PROJECT_ROOT / "src" / "infra_brain" / "etl" / "base.py"
    EXEMPT = {"__init__", "base", "llm_base"}

    if base_py.exists() and "build_callbacks" not in base_py.read_text(encoding="utf-8"):
        return error(
            name,
            "etl/base.py (ETLConnector) does not reference build_callbacks — ALL agents unwired",
            fix_hint="Restore build_callbacks() call in ETLConnector.__init__",
        )

    # Patterns that confirm callbacks are wired (direct or via inheritance chain).
    WIRED_PATTERNS = ("ETLConnector", "BaseAgent", "LLMAgent", "build_callbacks")

    unwired = []
    for f in sorted(agents_dir.glob("*.py")):
        if f.stem in EXEMPT or f.stem.startswith("_"):
            continue
        src = f.read_text(encoding="utf-8")
        # Skip pure re-export files (no class definition → no agent logic to wire).
        if not re.search(r"^class ", src, re.MULTILINE):
            continue
        if not any(p in src for p in WIRED_PATTERNS):
            unwired.append(f.stem)
    if unwired:
        return error(
            name,
            f"Agent(s) neither inherit BaseAgent nor import build_callbacks: {unwired}",
            fix_hint="Inherit BaseAgent or call build_callbacks() in __init__",
        )
    return ok(name, "All agents inherit BaseAgent (which wires build_callbacks)")


# ---------------------------------------------------------------------------
# Check 5: Migration State
# ---------------------------------------------------------------------------
def check_migration_state() -> CheckResult:
    name = "Migration State"
    python = _find_python()

    # `alembic heads` requires no DB — just reads migration files.
    rc2, stdout2, stderr2 = _run([python, "-m", "alembic", "heads"], cwd=str(PROJECT_ROOT))
    heads = [
        ln.strip()
        for ln in stdout2.splitlines()
        if ln.strip() and not ln.strip().startswith("INFO")
    ]
    if rc2 != 0 or len(heads) == 0:
        return warn(
            name,
            f"alembic heads failed: {(stdout2 + stderr2).strip()[:200]}",
            fix_hint="Check alembic.ini path or run alembic heads manually",
        )
    if len(heads) > 1:
        return error(
            name,
            f"Expected 1 migration head, found {len(heads)}: {heads}",
            fix_hint="Run `alembic merge heads` to resolve the branch conflict",
        )

    # `alembic check` requires a live DB — degrade to WARN when DB is unavailable.
    rc, stdout, stderr = _run([python, "-m", "alembic", "check"], cwd=str(PROJECT_ROOT))
    if rc != 0:
        combined = (stdout + stderr).strip()
        # Degrade to WARN when the DB is simply unavailable/unconfigured — no
        # connection ("Connection refused"/OperationalError) OR no DSN set at all
        # ("POSTGRES_URL is required", the pydantic ValidationError raised when
        # the audit runs locally without a .env). Neither is model drift.
        _no_db_signatures = (
            "Connection refused",
            "OperationalError",
            "could not connect",
            "POSTGRES_URL is required",
        )
        if any(sig in combined for sig in _no_db_signatures):
            return warn(
                name,
                f"No DB available — skipped ORM drift check (single head: {heads[0][:12]}...)",
                fix_hint="Run `alembic check` with POSTGRES_URL set to verify full migration state",
            )
        return error(
            name,
            f"alembic check failed: {combined[:300]}",
            fix_hint="Run /migration-create to generate a migration for the model drift",
        )

    return ok(name, f"Clean — 1 head, no ORM drift ({heads[0][:12]}...)")


# ---------------------------------------------------------------------------
# Check 6: Alembic env.py Lock Timeout
# ---------------------------------------------------------------------------
def check_alembic_lock_timeout() -> CheckResult:
    name = "Alembic env.py Lock Timeout"
    env_py = PROJECT_ROOT / "alembic" / "env.py"
    if not env_py.exists():
        return error(name, f"alembic/env.py not found at {env_py}")

    if "lock_timeout" not in env_py.read_text(encoding="utf-8"):
        return warn(
            name,
            "alembic/env.py has no lock_timeout — a migration that can't acquire a lock "
            "will hang indefinitely, blocking all collection runs",
            fix_hint=(
                "Add to run_migrations_online() before context.configure():\n"
                "    connection.execute(text(\"SET lock_timeout = '4s'\"))"
            ),
        )
    return ok(name, "lock_timeout is configured in alembic/env.py")


# ---------------------------------------------------------------------------
# Check 7: k8s Manifest Validation
# ---------------------------------------------------------------------------
def check_k8s_manifests() -> CheckResult:
    name = "k8s Manifest Validation"
    k8s_dir = PROJECT_ROOT / "k8s"
    if not k8s_dir.exists():
        return warn(name, "k8s/ directory not found — skipping")

    errors: list[str] = []
    warnings: list[str] = []

    scheduler_yaml = k8s_dir / "scheduler.yaml"
    if scheduler_yaml.exists():
        for m in re.finditer(r"\breplicas:\s*(\d+)", scheduler_yaml.read_text(encoding="utf-8")):
            if int(m.group(1)) > 1:
                errors.append(
                    f"scheduler.yaml replicas={m.group(1)} — APScheduler 3.x has no inter-process "
                    "lock; every job runs N times"
                )

    agent_core_yaml = k8s_dir / "agent-core.yaml"
    if agent_core_yaml.exists():
        content = agent_core_yaml.read_text(encoding="utf-8")
        for path in re.findall(r"livenessProbe.*?path:\s*(\S+)", content, re.DOTALL):
            if path.strip() == "/health":
                errors.append(
                    "agent-core.yaml livenessProbe uses /health (DB+Redis) — must be "
                    "/healthz (zero I/O); DB blips restart pods instead of pulling from LB"
                )

    for yaml_file in sorted(k8s_dir.glob("*.yaml")):
        if re.search(r"image:.*:latest", yaml_file.read_text(encoding="utf-8")):
            warnings.append(f"{yaml_file.name}: :latest image tag")

    if errors:
        return error(name, "; ".join(errors))
    if warnings:
        return warn(
            name, "; ".join(warnings), fix_hint="Pin image tags to a specific SHA or semver"
        )
    return ok(name, "scheduler replicas=1, liveness=/healthz, no :latest tags")


# ---------------------------------------------------------------------------
# Check 8: Environment Parity
# ---------------------------------------------------------------------------
def check_env_parity() -> CheckResult:
    name = "Environment Parity"
    python = _find_python()
    rc, stdout, stderr = _run(
        [python, "-m", "pytest", "tests/test_env_example_parity.py", "-q", "--tb=short"],
        cwd=str(PROJECT_ROOT),
    )
    if rc == 0:
        return ok(name, ".env.example documents all Settings fields")
    return error(
        name,
        f"Env parity test failed:\n{(stdout + stderr).strip()}",
        fix_hint="Add missing fields to .env.example (never edit .env directly)",
    )


# ---------------------------------------------------------------------------
# Check 9: CLAUDE.md Staleness
# ---------------------------------------------------------------------------
def check_claude_md_staleness(fix: bool = False) -> CheckResult:
    name = "CLAUDE.md Staleness"
    claude_md = PROJECT_ROOT / "CLAUDE.md"

    if not claude_md.exists():
        return error(name, "CLAUDE.md not found")

    python = _find_python()
    rc, stdout, stderr = _run(
        [python, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=str(PROJECT_ROOT),
    )
    actual_tests: int | None = None
    for line in (stdout + stderr).splitlines():
        m = re.search(r"(\d+)\s+tests? collected", line)
        if m:
            actual_tests = int(m.group(1))
            break

    actual_agents: int | None = None
    _data = _introspect()
    if _data is not None:
        actual_agents = len(_data["registry"])

    content = claude_md.read_text(encoding="utf-8")
    stale: list[str] = []

    if actual_tests is not None:
        for m in re.finditer(r"(\d+)\s+tests?", content, re.IGNORECASE):
            if int(m.group(1)) != actual_tests:
                stale.append(f"test count: doc={m.group(1)}, actual={actual_tests}")

    if actual_agents is not None:
        for m in re.finditer(r"(\d+)\s+agents?", content, re.IGNORECASE):
            if int(m.group(1)) != actual_agents:
                stale.append(f"agent count: doc={m.group(1)}, actual={actual_agents}")

    if stale and fix:
        new_content = content
        if actual_tests is not None:
            new_content = re.sub(
                r"\b(\d+)(\s+tests?)\b",
                lambda mm: (
                    f"{actual_tests}{mm.group(2)}"
                    if int(mm.group(1)) != actual_tests
                    else mm.group(0)
                ),
                new_content,
                flags=re.IGNORECASE,
            )
        if actual_agents is not None:
            new_content = re.sub(
                r"\b(\d+)(\s+agents?)\b",
                lambda mm: (
                    f"{actual_agents}{mm.group(2)}"
                    if int(mm.group(1)) != actual_agents
                    else mm.group(0)
                ),
                new_content,
                flags=re.IGNORECASE,
            )
        claude_md.write_text(new_content, encoding="utf-8")
        return ok(name, f"Fixed: {'; '.join(stale)}")

    if stale:
        return warn(
            name,
            "; ".join(stale),
            fix_hint="Run `python .claude/scripts/dev_status.py --fix` to auto-update",
        )
    counts = []
    if actual_tests is not None:
        counts.append(f"{actual_tests} tests")
    if actual_agents is not None:
        counts.append(f"{actual_agents} agents")
    return ok(name, f"Counts are accurate ({', '.join(counts)})")


# ---------------------------------------------------------------------------
# Check 10: SQL Validator
# ---------------------------------------------------------------------------
def check_sql_validator() -> CheckResult:
    name = "SQL Validator"
    python = _find_python()
    rc, stdout, stderr = _run(
        [python, "-m", "pytest", "tests/test_dashboard_sql_columns.py", "-q", "--tb=short"],
        cwd=str(PROJECT_ROOT),
    )
    if rc == 0:
        return ok(name, "All dashboard/chat SQL column references resolve to real ORM columns")
    return error(
        name,
        f"SQL column validator failed:\n{(stdout + stderr).strip()}",
        fix_hint="Run /validate-sql for the detailed report; ORM is source of truth",
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _print_report(results: list[CheckResult]) -> int:
    width = 62
    print()
    print(_bold("=" * width))
    print(_bold("  infra-brain /dev-status"))
    print(_bold("=" * width))

    worst = 0
    for r in results:
        if r.status == "OK":
            icon = _green("  OK   ")
            print(f"{icon} {r.name}")
            print(f"         {r.message}")
        elif r.status == "WARN":
            icon = _yellow("  WARN ")
            print(f"{icon} {r.name}")
            for part in r.message.split("\n"):
                print(f"         {part}")
            if r.fix_hint:
                print(f"         -> {r.fix_hint}")
            worst = max(worst, 1)
        else:
            icon = _red("  ERR  ")
            print(f"{icon} {r.name}")
            for part in r.message.split("\n"):
                print(f"         {part}")
            if r.fix_hint:
                print(f"         -> {r.fix_hint}")
            worst = max(worst, 2)
        print()

    ok_n = sum(1 for r in results if r.status == "OK")
    warn_n = sum(1 for r in results if r.status == "WARN")
    err_n = sum(1 for r in results if r.status == "ERROR")
    summary = f"  {ok_n} OK  {warn_n} WARN  {err_n} ERROR"

    print(_bold("-" * width))
    if worst == 0:
        print(_green(summary))
    elif worst == 1:
        print(_yellow(summary))
    else:
        print(_red(summary))
    print(_bold("=" * width))
    print()
    return worst


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="infra-brain developer tooling health audit")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-update hardcoded counts in CLAUDE.md (dry-run by default)",
    )
    args = parser.parse_args(argv)

    results = [
        check_agent_registry(),
        check_test_coverage(),
        check_schedule_collisions(),
        check_callbacks_wiring(),
        check_migration_state(),
        check_alembic_lock_timeout(),
        check_k8s_manifests(),
        check_env_parity(),
        check_claude_md_staleness(fix=args.fix),
        check_sql_validator(),
    ]
    return _print_report(results)


if __name__ == "__main__":
    sys.exit(main())
