"""Headless runner (P6, convergence plan Phase 6).

A scheduler-driven agent that runs standing tasks (drift triage, self-review,
EOL reporting) with no interactive session or human present. Its mutating
actions are attributed as ``mcp:headless-runner`` via a real MCP HTTP call
with a scoped key (D10) — see ``mcp_client.py``'s docstring for why this is
a different transport than the dashboard chat agent's in-process tools.
"""
