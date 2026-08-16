"""PostToolUse: warn when a new agent file is created without a matching test file.

Runs on any Write/Edit to src/infra_brain/agents/*.py. If the corresponding
tests/agents/test_<name>.py doesn't exist, emits a warning (exit 1).
Does NOT block (exit 1, not 2) — the test file may be created next. But repeated
saves to an agent with no test escalate to a reminder.
"""
import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
file_path = payload.get("tool_input", {}).get("file_path", "")

if tool not in ("Edit", "Write"):
    sys.exit(0)

p = Path(file_path)

# Only care about src/infra_brain/agents/*.py (not base.py, __init__.py)
if "agents" not in p.parts:
    sys.exit(0)
if p.name.startswith("_") or p.name in ("base.py", "llm_base.py"):
    sys.exit(0)
if not str(file_path).replace("\\", "/").find("/infra_brain/agents/") >= 0:
    sys.exit(0)
if p.suffix != ".py":
    sys.exit(0)

# Resolve project root from the file path
parts = p.parts
try:
    src_idx = next(i for i, part in enumerate(parts) if part == "src")
    project_root = Path(*parts[:src_idx])
except StopIteration:
    sys.exit(0)

agent_name = p.stem  # e.g. "linux" from "linux.py"
test_file = project_root / "tests" / "agents" / f"test_{agent_name}.py"

if not test_file.exists():
    print(f"test-coverage-guard: WARNING — no test file for '{agent_name}' agent")
    print(f"  Expected: {test_file}")
    print(f"  Run /agent-scaffold {agent_name} to generate a test file from the project template.")
    sys.exit(1)

print(f"test-coverage-guard: OK — test file exists for '{agent_name}'")
