#!/usr/bin/env python3
"""PostToolUse hook: run targeted tests when editing the highest-blast-radius files/package.
config.py, db/models/ (any module), db/session.py are all widely imported.
A breaking change here cascades silently into unexpected failures elsewhere.
"""
import json
import os
import subprocess
import sys

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")

HIGH_BLAST_RADIUS = (
    "src/infra_brain/config.py",
    "src/infra_brain/db/session.py",
)

if not (fp.endswith(HIGH_BLAST_RADIUS) or "src/infra_brain/db/models/" in fp):
    sys.exit(0)

for candidate in (".venv/Scripts/python.exe", ".venv/bin/python", sys.executable):
    if os.path.exists(candidate):
        python = candidate
        break
else:
    python = sys.executable

fast_tests = [
    "tests/test_config.py",
    "tests/db/test_models.py",
    "tests/test_migration.py",
    "tests/test_env_example_parity.py",
]

print(f"\n[blast-radius-guard] '{fp}' is high-blast-radius — running targeted tests...")
result = subprocess.run(
    [python, "-m", "pytest"] + fast_tests + ["-q", "--tb=short"],
)
if result.returncode != 0:
    print(
        "\n[blast-radius-guard] TESTS FAILED — fix before continuing.\n"
        "This file has 8-12 downstream dependents; failures cascade widely.",
        file=sys.stderr,
    )
    sys.exit(1)
