#!/usr/bin/env python3
"""PostToolUse hook: when a db/models/ module changes, run alembic check to catch migration drift."""
import json
import os
import subprocess
import sys

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")

if "db/models/" not in fp:
    sys.exit(0)

for candidate in (".venv/Scripts/python.exe", ".venv/bin/python", sys.executable):
    if os.path.exists(candidate):
        python = candidate
        break
else:
    python = sys.executable

result = subprocess.run(
    [python, "-m", "alembic", "check"],
    capture_output=True, text=True,
)

if result.returncode != 0:
    print(
        "\n[alembic-check] WARNING: ORM models have drifted from migrations.\n"
        "Run /migration-create to generate a new migration before merging.\n"
        f"Details: {(result.stdout + result.stderr).strip()}",
        file=sys.stderr,
    )
    sys.exit(1)
else:
    print("[alembic-check] OK — migrations are in sync with ORM models.")
