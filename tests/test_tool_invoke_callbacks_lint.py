"""CI lint (item 1.3 / F-004.2): every direct tool .invoke() in agents/ must
carry config={"callbacks": ...} so the safety chain observes the call.

Static scan — no DB, no network. Runs in every MR pipeline via pytest.
"""

import re
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "infra_brain" / "agents"

# Matches direct tool-object invokes like `gitlab_file_tool.invoke(`.
_INVOKE_RE = re.compile(r"\w+_tool\.invoke\(")


def _call_text(text: str, open_paren_idx: int) -> str:
    """Return the full parenthesized call starting at the given '(' index."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx : i + 1]
    return text[open_paren_idx:]


def test_every_tool_invoke_carries_callbacks():
    """Fails today: inventory_reconcile.py has 3 bare .invoke() calls
    (lines 52-54, 61, 136-138)."""
    offenders: list[str] = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in _INVOKE_RE.finditer(text):
            call = _call_text(text, m.end() - 1)
            if "config=" not in call:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.name}:{line}")
    assert offenders == [], (
        "tool .invoke() without config={'callbacks': ...} — the safety chain "
        f"never observes these calls (F-004.2): {offenders}"
    )
