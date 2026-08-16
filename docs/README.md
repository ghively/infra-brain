# Documentation Index

## Start here

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the living architecture reference:
  agent registry, the declarative graph engine, the sweep pipeline, observability,
  RAG. The most-current single source of truth for how the system is built.
- **[READONLY-MODEL.md](READONLY-MODEL.md)** — the three-layer read-only enforcement
  model (structural, boundary-gate, audit) and a real incident writeup of the DLP
  layer catching something it was supposed to catch.
- **[MCP_SERVER.md](MCP_SERVER.md)** — full reference for the MCP tool surface any
  MCP-compatible client (Claude Code or otherwise) can connect through.
- **[USER_GUIDE.md](USER_GUIDE.md)** — install, configure, run, operate, troubleshoot.
- **[PATTERNS.md](PATTERNS.md)** — the codebase's own conventions (agent base
  classes, the pagination envelope, the `reason()` loop) — useful if you're reading
  or extending the code, not just running it.
- **[specs/2026-06-29-netdiscovery-agent-design.md](specs/2026-06-29-netdiscovery-agent-design.md)** —
  a full, self-contained design spec for one domain agent; the cleanest example in
  this repo of "goal → constraints → architecture → safety model → test plan."
- **[RETENTION-POLICY.md](RETENTION-POLICY.md)** — data retention windows and why.

## Architecture decision records

`decisions/` is the ADR archive — a real engineering-process log, not a polished
retrospective. Each file records a decision at the time it was made, including the
reasoning that turned out to be wrong. The two most substantial and recent:

- **[2026-08-11-graph-first-architecture.md](decisions/2026-08-11-graph-first-architecture.md)**
  and **[2026-08-10-graph-edge-authority-spec.md](decisions/2026-08-10-graph-edge-authority-spec.md)**
  — the design behind the current declarative graph engine.

The rest of `decisions/` covers earlier, narrower decisions (the dashboard
framework migration, LLM/embedding-provider choices, orchestration-tooling
redesigns, and similar) — worth reading if you're curious how a specific piece
came to be, not required to understand the system as it stands today.

## Everything else

- **[onboarding-artifact.html](onboarding-artifact.html)** — a standalone, designed
  HTML onboarding page (open it in a browser, not a markdown viewer).
- **[BACKLOG.md](BACKLOG.md)** — an honest, short list of what's documented as open
  rather than fixed/verified.
- **[DE-BRITTLING-PLAN.md](DE-BRITTLING-PLAN.md)** — a condensed lessons-learned
  writeup from a real production incident (model/database schema drift shipping
  as a "healthy" deploy).
- `knowledge-graph-convergence-nodes.md` — point-in-time working notes, kept for
  provenance rather than as current reference.
