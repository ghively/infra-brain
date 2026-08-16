"""Mint (or rotate) the Buzz ACP bridge's scoped MCP key (D10 pattern).

Same reasoning as ``mint_headless_runner_key.py``: the caller is not a human
Claude Code session, so it holds its own scoped, independently-revocable key
rather than reusing the bootstrap/dashboard key. Actions attribute to this
key's name (``buzz-acp``) in the audit log.

Scoped to the full read-only tool catalog (``mcp_auth.READONLY_TOOL_NAMES``)
and nothing else -- the Buzz relay this bridges to is membership-gated and
tailnet-only, but the ACP harness itself runs with ``permission-mode:
bypass-permissions`` (no per-tool-call confirmation), so the safety boundary
for this integration is enforced server-side, by what this key is allowed to
call, not by any client-side prompt. No mutation tool names here by design;
widen deliberately later if a real write use case shows up.

Usage (inside the running app container -- needs DB access)::

    docker compose -p infra-brain -f docker/docker-compose.yml \
        -f docker/docker-compose.homelab.yml exec app \
        python scripts/mint_buzz_acp_key.py

Prints the raw token ONCE (never stored raw, same "shown once" convention as
every other key-creation path in this repo).

Idempotent by name: re-running when a non-revoked "buzz-acp" key already
exists does NOT mint a second key -- use --rotate to revoke the existing one
and mint a fresh key instead.
"""

from __future__ import annotations

import argparse
import sys

BUZZ_ACP_KEY_NAME = "buzz-acp"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint (or rotate) the Buzz ACP bridge's scoped MCP key."
    )
    parser.add_argument(
        "--rotate", action="store_true",
        help="revoke any existing non-revoked 'buzz-acp' key first, then mint a fresh one",
    )
    args = parser.parse_args(argv)

    from infra_brain import mcp_auth
    from infra_brain.db.models import McpApiKey
    from infra_brain.db.session import get_session

    with get_session() as session:
        existing = (
            session.query(McpApiKey)
            .filter(McpApiKey.name == BUZZ_ACP_KEY_NAME, McpApiKey.revoked_at.is_(None))
            .one_or_none()
        )
        if existing is not None:
            if not args.rotate:
                print(
                    f"an active '{BUZZ_ACP_KEY_NAME}' key already exists "
                    f"(id={existing.id}) -- pass --rotate to revoke it and mint a fresh one"
                )
                return 1
            mcp_auth.revoke_key(session, existing.id)
            session.commit()
            print(f"revoked existing key id={existing.id}")

        row, raw = mcp_auth.create_key(
            session,
            name=BUZZ_ACP_KEY_NAME,
            allowed_tools=mcp_auth.READONLY_TOOL_NAMES,
            created_by="mint_buzz_acp_key.py",
        )
        session.commit()
        print(f"minted key id={row.id} name={row.name}")
        print(f"allowed_tools: {len(mcp_auth.READONLY_TOOL_NAMES)} read-only tools")
        print(f"RAW TOKEN (shown once, not stored): {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
