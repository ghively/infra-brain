"""Generate AGENTS.md (the domain-agent roster) from the AgentSpec registry.

Usage:
    .venv/bin/python scripts/gen_agents_md.py

The committed AGENTS.md must always match this generator's output —
tests/etl/test_agent_spec.py::test_agents_md_matches_generator_output keeps
it honest in CI. To change the roster, change the agents' ``spec``
declarations (or this generator), never AGENTS.md by hand.
"""

import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_TIER_LABEL = {
    "collector": "Collector",
    "reconciler": "Reconciler",
    "reasoner": "Reasoner",
    "reporter": "Reporter",
    "on_demand": "On-demand",
}
_TIER_ORDER = ["collector", "reconciler", "reasoner", "reporter", "on_demand"]


def _staleness(td: timedelta | None) -> str:
    if td is None:
        return "—"
    hours = td.total_seconds() / 3600
    if hours % 24 == 0 and hours >= 48:
        return f"{int(hours // 24)}d"
    return f"{hours:g}h"


def render() -> str:
    from infra_brain.etl.spec import agent_specs

    specs = agent_specs()
    ordered = sorted(
        specs.values(),
        key=lambda s: (_TIER_ORDER.index(s.tier.value), s.domain),
    )
    lines = [
        "# infra-brain Domain Agents",
        "",
        "> **GENERATED — edit `scripts/gen_agents_md.py` (or the agents' `spec`",
        "> declarations), never this file.** Regenerate with",
        "> `.venv/bin/python scripts/gen_agents_md.py`; a CI test",
        "> (`tests/etl/test_agent_spec.py`) fails when this file is stale.",
        "",
        f"{len(specs)} dispatchable domains. Each agent class declares a frozen",
        "`AgentSpec` (`src/infra_brain/etl/spec.py`) — the single source of truth for",
        "domain, tier, cron schedule, freshness window, and hook behavior. See",
        "`docs/ARCHITECTURE.md` for the AgentSpec contract and tier semantics.",
        "",
        "A **retired** domain is switched off by standing decision: its upstream system",
        "does not exist in this environment. It is not scheduled, not a sweep member,",
        "not freshness-monitored and not dispatchable — but it stays registered,",
        "importable and testable, and the cron shown is the one it would resume on.",
        "Turn one back on with `COLLECTION_REVIVED_DOMAINS=<domain>` (no code change).",
        "",
        "| Domain | Tier | Schedule (cron) | Max staleness | Skip hook | Retired |",
        "|---|---|---|---|---|---|",
    ]
    for s in ordered:
        lines.append(
            f"| {s.domain} | {_TIER_LABEL[s.tier.value]} | "
            f"{s.schedule or '— (on-demand / hook-driven)'} | "
            f"{_staleness(s.max_staleness)} | "
            f"{'yes' if s.skip_hook else 'no'} | "
            f"{'**yes**' if s.retired else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    out = REPO_ROOT / "AGENTS.md"
    out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
