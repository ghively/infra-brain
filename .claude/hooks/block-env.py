#!/usr/bin/env python3
"""PreToolUse hook: hard-block edits to .env (contains live secrets).
Edit .env.example instead, then update Bitwarden + config.py.
Exit code 2 = hard block.
"""
import json
import sys

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "").replace("\\", "/").rstrip("/")

blocked = (
    fp == ".env"
    or fp.endswith("/.env")
    or fp == ".env.local"
    or fp.endswith("/.env.local")
)

if blocked:
    print(
        f"BLOCKED: cannot edit '{fp}' — it contains live secrets pulled from Bitwarden.\n"
        "Edit '.env.example' to document the variable, update the Bitwarden project,\n"
        "and 'src/infra_brain/config.py' (Settings class) instead.",
        file=sys.stderr,
    )
    sys.exit(2)
