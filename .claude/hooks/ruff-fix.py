#!/usr/bin/env python3
"""PostToolUse hook: run ruff --fix on any edited Python file in src/ or tests/."""
import json
import os
import subprocess
import sys

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "")

if not fp.endswith(".py"):
    sys.exit(0)

parts = fp.replace("\\", "/")
if not any(seg in parts for seg in ("src/", "tests/", "scripts/")):
    sys.exit(0)

for candidate in (".venv/Scripts/python.exe", ".venv/bin/python", sys.executable):
    if os.path.exists(candidate):
        python = candidate
        break
else:
    python = sys.executable

result = subprocess.run(
    [python, "-m", "ruff", "check", "--fix", fp, "--quiet"],
    capture_output=True, text=True,
)
if result.stdout.strip():
    print(result.stdout.strip())
if result.returncode > 1:
    print(f"ruff error: {result.stderr.strip()}", file=sys.stderr)
