#!/usr/bin/env python3
"""PostToolUse hook: warn when a new Settings field in config.py has no matching
env var in .env.example.

Exit 1 = warning (not a hard block — the .env.example edit may follow immediately).
"""
import json
import re
import sys
from pathlib import Path

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")

if not fp.endswith("src/infra_brain/config.py"):
    sys.exit(0)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
config_path = PROJECT_ROOT / "src" / "infra_brain" / "config.py"
env_example_path = PROJECT_ROOT / ".env.example"

if not config_path.exists() or not env_example_path.exists():
    sys.exit(0)

config_src = config_path.read_text(encoding="utf-8")
env_src = env_example_path.read_text(encoding="utf-8")

# Extract Settings fields: 4-space-indented `name: type` or `name: type = default`
settings_fields = set(re.findall(r"^    ([a-z_]+)\s*:", config_src, re.MULTILINE))

# Extract keys documented in .env.example (uppercase lines before the =)
env_keys = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", env_src, re.MULTILINE))
# Also catch commented-out examples like # DATABASE_URL=...
env_keys |= set(re.findall(r"^#\s*([A-Z_][A-Z0-9_]*)=", env_src, re.MULTILINE))

# Map field names to expected env var names (snake_case → UPPER_CASE)
missing = [
    field
    for field in sorted(settings_fields)
    if field.upper() not in env_keys
    and not field.startswith("_")
    # Skip fields that are clearly internal/computed
    and field not in {"model_config"}
]

if missing:
    print(
        f"\n[env-parity-guard] {len(missing)} Settings field(s) in config.py "
        f"have no matching key in .env.example:\n"
        + "\n".join(f"  - {f}  (expected key: {f.upper()})" for f in missing)
        + "\n\nIf this is a new secret/config, add it to .env.example before committing.\n"
        "See CLAUDE.md → Secrets and Configuration for the full workflow.",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    f"[env-parity-guard] OK — {len(settings_fields)} Settings fields, "
    f"all documented in .env.example",
    file=sys.stderr,
)
