"""Mint (or rotate) the headless runner's scoped MCP key (P6.1/P6.2, D10).

D10: "Headless runner holds its own scoped MCP key. Actions attribute to
mcp:headless-runner and are independently revocable, rather than stamping
direct:unattributed." This is the one-shot bootstrap for that key --
mirrors mcp_auth.py's own ``--bootstrap`` CLI (create_bootstrap_key), but
scoped to exactly the tool set the headless runner needs (reads are not
gated by MCP scope at all -- the runner's read tools call the DB directly,
same as the dashboard chat agent's; only these four mutations go over the
MCP transport):

    create_gitlab_issue, record_governance_event, record_client_state,
    propose_instinct

Deliberately NOT included: seed_*, resolve_drift_events, approve_proposal --
an unattended nightly/weekly runner should never seed inventory, bulk-close
drift, or approve a human-review-gated proposal.

Usage (inside the running app container -- needs DB access)::

    docker compose -p infra-brain -f docker/docker-compose.yml \
        -f docker/docker-compose.homelab.yml exec app \
        python scripts/mint_headless_runner_key.py

Prints the raw token ONCE (never stored raw, same "shown once" convention
as every other key-creation path in this repo). Put it in
HEADLESS_RUNNER_MCP_TOKEN (.env / Bitwarden) and restart the scheduler
service to pick it up.

Idempotent by name: re-running when a non-revoked "headless-runner" key
already exists does NOT mint a second key (a duplicate active key would be
confusing to audit and impossible to tell apart in the MCP key list) --
use --rotate to revoke the existing one and mint a fresh key instead.
"""

from __future__ import annotations

import argparse
import sys

HEADLESS_RUNNER_KEY_NAME = "headless-runner"
HEADLESS_RUNNER_ALLOWED_TOOLS = [
    "create_gitlab_issue",
    "record_governance_event",
    "record_client_state",
    "propose_instinct",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint (or rotate) the headless runner's scoped MCP key."
    )
    parser.add_argument(
        "--rotate", action="store_true",
        help="revoke any existing non-revoked 'headless-runner' key first, then mint a fresh one",
    )
    args = parser.parse_args(argv)

    from infra_brain import mcp_auth
    from infra_brain.db.models import McpApiKey
    from infra_brain.db.session import get_session

    with get_session() as session:
        existing = (
            session.query(McpApiKey)
            .filter(McpApiKey.name == HEADLESS_RUNNER_KEY_NAME, McpApiKey.revoked_at.is_(None))
            .one_or_none()
        )
        if existing is not None:
            if not args.rotate:
                print(
                    f"an active '{HEADLESS_RUNNER_KEY_NAME}' key already exists "
                    f"(id={existing.id}) -- pass --rotate to revoke it and mint a fresh one"
                )
                return 1
            mcp_auth.revoke_key(session, existing.id)
            session.commit()
            print(f"revoked existing key id={existing.id}")

        row, raw = mcp_auth.create_key(
            session,
            name=HEADLESS_RUNNER_KEY_NAME,
            allowed_tools=HEADLESS_RUNNER_ALLOWED_TOOLS,
            created_by="mint_headless_runner_key.py",
        )
        session.commit()
        print(f"minted key id={row.id} name={row.name}")
        print(f"allowed_tools: {sorted(HEADLESS_RUNNER_ALLOWED_TOOLS)}")
        print(f"RAW TOKEN (shown once, not stored): {raw}")
        print("Set HEADLESS_RUNNER_MCP_TOKEN to this value and restart the scheduler service.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
