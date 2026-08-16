"""MR-J safety-review follow-up: guard against a future edit turning a
read-only PowerShell one-liner in agents/windows.py into a mutating one.

The WinRM lane (tools/winrm_client.py + agents/windows.py's `client.run_ps(...)`
calls) is documented in docs/READONLY-MODEL.md as "read-only by CONVENTION" —
it bypasses the boundary gate/DLP/audit callback chain entirely (pywinrm calls
are not routed through build_callbacks()), so code review + this static check
are the ONLY compensating controls. This test statically scans every string
literal passed to `.run_ps(` in agents/windows.py for PowerShell verbs that
would mutate host state, and fails if any are found.
"""

import ast
import re
from pathlib import Path

WINDOWS_AGENT_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "infra_brain" / "agents" / "windows.py"
)

# PowerShell verbs that indicate a mutation (installing, changing, removing,
# starting/stopping a service, granting permissions, etc.). Read-only verbs
# (Get-, Search, Select-Object, Sort-Object, Where-Object, ForEach-Object,
# ConvertTo-Json) are NOT matched. ``New-Object`` is deliberately excluded —
# it only instantiates an in-memory .NET/COM object (e.g. the Windows Update
# session below) and has no host side effect on its own, unlike
# ``New-LocalUser``/``New-SmbShare``/etc.
_MUTATING_VERB_RE = re.compile(
    r"\bSet-|\bNew-(?!Object\b)|\bRemove-|\bAdd-|\bInstall-|\bUninstall-|\bGrant-|\bRevoke-|"
    r"\bClear-|\bStop-|\bStart-|\bRestart-|\bEnable-|\bDisable-|\bRename-|\bMove-|\bCopy-|"
    r"\bOut-File\b|\bInvoke-WebRequest\b|\bInvoke-Expression\b",
    re.IGNORECASE,
)


def _extract_run_ps_string_literals() -> list[str]:
    """Return every string literal (or f-string constant piece) passed as the
    first argument to a `.run_ps(...)` call in agents/windows.py, including
    ones built via `+`/implicit adjacent-string concatenation."""
    tree = ast.parse(WINDOWS_AGENT_PATH.read_text(encoding="utf-8"))
    literals: list[str] = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "run_ps":
            continue
        if not node.args:
            continue
        literals.append(_flatten_string_expr(node.args[0]))

        # ps_cmd = "..." ; result = client.run_ps(ps_cmd) — the WMI/service
        # collectors build the string in a local variable first. Walk the
        # whole function body containing this call for any `ps_cmd = "..."`
        # / `ps_cmd = (...)` assignment as a best-effort fallback.
    # Fallback: also capture every assignment whose target name contains
    # "ps_cmd" or "ps_" (covers the local-variable-then-call pattern used by
    # _collect_services/_collect_software above the newer inline calls).
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any("ps_cmd" in n for n in names):
                literals.append(_flatten_string_expr(node.value))

    return [lit for lit in literals if lit]


def _flatten_string_expr(node) -> str:
    """Best-effort: concatenate every string constant found anywhere inside
    *node* (handles `"a" "b"` implicit concat, `"a" + "b"`, and parenthesized
    multi-line strings)."""
    parts: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            parts.append(inner.value)
    return " ".join(parts)


def test_no_mutating_powershell_verbs_in_windows_agent():
    literals = _extract_run_ps_string_literals()
    assert literals, "expected to find at least one run_ps(...) call in agents/windows.py"

    violations = []
    for text in literals:
        match = _MUTATING_VERB_RE.search(text)
        if match:
            violations.append((match.group(0), text[:120]))

    assert violations == [], (
        "Mutating PowerShell verb found in a WinRM one-liner — this lane bypasses the "
        "boundary gate entirely (see docs/READONLY-MODEL.md), so this must never happen: "
        f"{violations}"
    )


def test_windows_update_search_uses_search_not_install():
    """Specifically pin the Windows Update call to the read-only WUA
    ``Search()`` method (never ``Download()``/``Install()``)."""
    literals = _extract_run_ps_string_literals()
    update_calls = [t for t in literals if "CreateUpdateSearcher" in t]
    assert update_calls, "expected the CreateUpdateSearcher() call to be present"
    for text in update_calls:
        assert ".Search(" in text
        assert ".Download(" not in text
        assert ".Install(" not in text
