#!/usr/bin/env python3
"""PreToolUse hook: warn before editing safety-critical callback files.
Exit 1 = warning shown to Claude (not a hard block).
"""
import json
import sys

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")

SAFETY_CRITICAL = {
    "src/infra_brain/callbacks/readonly.py": (
        "ReadOnlyToolValidator — enforces the read-only guarantee for ALL tool calls.\n"
        "  Never remove a 'raise' statement or weaken a guard condition.\n"
        "  Run: pytest tests/callbacks/test_readonly.py -v"
    ),
    "src/infra_brain/callbacks/dlp.py": (
        "DLPCallbackHandler — scans all LLM outputs for PII (Luhn/PAN).\n"
        "  Do not remove pattern checks.\n"
        "  Run: pytest tests/callbacks/test_dlp.py -v"
    ),
    "src/infra_brain/callbacks/registry.py": (
        "build_callbacks() — wires ALL safety callbacks into every agent.\n"
        "  Every new agent MUST call build_callbacks().\n"
        "  Verify the full callback chain is still assembled after any change."
    ),
    "src/infra_brain/supervisor.py": (
        "LangGraph supervisor — routes all 23 domain agents.\n"
        "  A routing bug silently misdirects work system-wide.\n"
        "  Run: pytest tests/agents/test_supervisor.py -v"
    ),
}

for path, warning in SAFETY_CRITICAL.items():
    if fp.endswith(path):
        print(
            f"\n[safety-guard] SAFETY-CRITICAL FILE: {path}\n"
            f"  {warning}\n"
            "  Consider invoking the lc-safety-reviewer subagent before proceeding.",
            file=sys.stderr,
        )
        sys.exit(1)
