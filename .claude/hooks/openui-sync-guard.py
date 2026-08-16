"""PostToolUse: remind to run /openui-sync when a dashboard API route file is
modified.

P7.2 (2026-08-01): this used to fire only on dashboard_api.py, but that file
is now a re-export shim only (CLAUDE.md: "Do NOT add handler logic here") --
every real route handler lives in api/routers/*.py, which this hook never
matched, so the reminder had gone effectively dead. Now matches any file
under api/routers/ (where endpoint surface actually changes) as well as
dashboard_api.py itself, in case it ever regains real logic.
"""
import json
import sys

payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
file_path = payload.get("tool_input", {}).get("file_path", "").replace("\\", "/")

if tool not in ("Edit", "Write"):
    sys.exit(0)

if "dashboard_api.py" not in file_path and "/api/routers/" not in file_path:
    sys.exit(0)

print(
    "\n⚡ OpenUI sync reminder: dashboard_api.py was modified.\n"
    "   Run /openui-sync to verify the component library is still in sync\n"
    "   with all available API endpoints.\n",
    file=sys.stderr,
)
sys.exit(0)
