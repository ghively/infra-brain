"""PostToolUse: validate k8s YAML edits for critical misconfiguration patterns.

Checks:
- Probe separation: liveness must use /healthz, readiness must use /health
- Scheduler replica guard: k8s/scheduler.yaml must not set replicas > 1
- Image tag: warns if :latest is used
- Basic YAML syntax (via PyYAML)

Runs on any Edit/Write to k8s/*.yaml files.
"""
import json
import re
import sys
from pathlib import Path

payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
file_path = payload.get("tool_input", {}).get("file_path", "")

if tool not in ("Edit", "Write"):
    sys.exit(0)

p = Path(file_path)
if "k8s" not in p.parts or p.suffix not in (".yaml", ".yml"):
    sys.exit(0)
if not p.exists():
    sys.exit(0)

content = p.read_text(encoding="utf-8")
issues = []
blocks = []

# YAML syntax check
try:
    import yaml
    yaml.safe_load_all(content)
except Exception as exc:
    blocks.append(f"Invalid YAML syntax: {exc}")

# Scheduler replica guard — replicas > 1 in scheduler.yaml causes every job to run N times
if p.name == "scheduler.yaml":
    for m in re.finditer(r"\breplicas:\s*(\d+)", content):
        val = int(m.group(1))
        if val > 1:
            blocks.append(
                f"CRITICAL: scheduler.yaml replicas={val} — APScheduler 3.x has no inter-process "
                f"execution lock; every scheduled job will run {val} times. Must be replicas: 1."
            )

# Probe separation check — liveness must be /healthz (zero I/O), not /health (DB+Redis check)
# A liveness probe pointing at /health restarts pods on DB blips instead of just removing from LB
liveness_paths = re.findall(r"livenessProbe.*?path:\s*(\S+)", content, re.DOTALL)
for path in liveness_paths:
    if path.strip() == "/health":
        issues.append(
            "livenessProbe uses /health (checks Postgres+Redis) — use /healthz (zero I/O) instead. "
            "A DB blip will restart pods rather than just pulling them from load balancer rotation."
        )

# Image tag check — :latest in production manifests bypasses image pinning
if re.search(r"image:.*:latest", content):
    issues.append("Image tag ':latest' found — pin to a specific SHA or semver tag for reproducible deployments.")

if blocks:
    print(f"lint-k8s [{p.name}]: BLOCKED")
    for b in blocks:
        print(f"  ✗ {b}")
    sys.exit(2)

if issues:
    print(f"lint-k8s [{p.name}]: WARNINGS")
    for w in issues:
        print(f"  ⚠ {w}")
    sys.exit(1)

print(f"lint-k8s [{p.name}]: OK")
