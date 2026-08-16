"""
Sandboxed READ-ONLY script execution for autonomous data gathering.
The LLM may generate PowerShell/Python/Bash; this runner refuses anything that
looks mutating, runs it with a timeout in a temp cwd, and audits every call.

Every script (allowed or denied) is persisted via scripts_store for audit/reuse.
"""

import hashlib
import logging
import re
import subprocess
import tempfile
from langchain_core.tools import ToolException, tool
from infra_brain.config import get_settings
from infra_brain import scripts_store

logger = logging.getLogger(__name__)

# Deny-list of mutating constructs across all three languages.
#
# NOTE: keyword boundaries use \b (matches at ANY word/non-word transition —
# whitespace, quote, comma, paren, end-of-string, ...) rather than a literal
# \s (whitespace only). Argv/list-style invocations such as
# subprocess.run(['rm', '-rf', '/']) or ['systemctl', 'stop', 'sshd'] put a
# quote/comma immediately after the keyword, never whitespace — a \s-based
# pattern silently lets those through. Multi-token constructs (dd if=,
# systemctl <verb>, reg add/delete) use a small separator class
# [\s,'"] between the tokens so both "dd if=..." (shell) and
# ['dd', 'if=...'] (argv list, where the tokens are separated by
# quote/comma/space) are caught. "format" additionally excludes an
# immediately-following '(' so str.format(...) calls aren't flagged.
_MUTATION_PATTERNS = re.compile(
    r"("
    # Filesystem mutations
    r"\brm\b|\brmdir\b|\bdel\b|\bRemove-Item\b"
    r"|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bNew-Item\b|\bSet-Item\b|\bClear-Content\b"
    # Disk/partition tools
    r"|\bmkfs\b|\bdd\b[\s,'\"]*if=|\bformat\b(?!\()|\bfdisk\b"
    # Python filesystem mutations
    r"|\bos\.remove\b|\bos\.unlink\b|\bos\.rmdir\b|\bshutil\.rmtree\b"
    # File open in write/append/exclusive mode
    r"|open\([^)]*['\"][waxWAX]"
    # SQL mutations
    r"|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bDROP\b|\bTRUNCATE\b|\bALTER\b|\bCREATE\b"
    # Shell redirection to root/system paths
    r"|>{1,2}\s*/"
    # Permission/ownership changes
    r"|\bchmod\b|\bchown\b"
    # Service management
    r"|\bsystemctl\b[\s,'\"]*(start|stop|restart|enable|disable)\b"
    # Windows registry mutations
    r"|\breg\b[\s,'\"]*add\b|\breg\b[\s,'\"]*delete\b"
    # Windows service/power management
    r"|\bStop-Service\b|\bStart-Service\b|\bRestart-Computer\b|\bshutdown\b"
    r")",
    re.IGNORECASE,
)

_INTERPRETERS: dict[str, list[str]] = {
    "powershell": ["pwsh", "-NonInteractive", "-NoProfile", "-Command"],
    "python": ["python", "-c"],
    "bash": ["bash", "-c"],
}


def _scan_for_mutations(script: str) -> None:
    """Raise PermissionError if script contains a mutating construct."""
    m = _MUTATION_PATTERNS.search(script)
    if m:
        raise PermissionError(f"[read-only] script blocked — mutating construct: {m.group(0)!r}")


def _persist_script(
    language: str,
    script: str,
    stdout: str = "",
    returncode: int | None = None,
    note: str = "",
) -> None:
    """
    Persist a script to the script store (best-effort — never raises).
    Called after every run, including denied and timed-out scripts.
    """
    digest = hashlib.sha256(script.encode()).hexdigest()
    name = f"script-{digest[:8]}"
    purpose_val = note if note else "ad-hoc"
    try:
        scripts_store.save_script(
            name=name,
            language=language,
            purpose=purpose_val,
            content=script,
            created_by_agent="script_runner",
            domain="script",
            stdout=stdout,
            returncode=returncode,
        )
    except Exception as exc:
        logger.warning("scripts_store.save_script failed (non-fatal): %s", exc)


@tool
def run_readonly_script_tool(language: str, script: str) -> dict:
    """
    Execute a READ-ONLY data-gathering script (powershell|python|bash).
    Refuses mutating scripts, runs with a timeout in a temp dir, and audits the call.
    Returns {stdout, stderr, returncode} or {error}.
    Gated on scripts_enabled config (default False).

    Every script (allowed or denied) is persisted via scripts_store for audit/reuse.
    """
    s = get_settings()
    if not getattr(s, "scripts_enabled", False):
        return {
            "error": (
                "script execution disabled (set SCRIPTS_ENABLED=true to allow read-only scripts)"
            )
        }

    if language not in _INTERPRETERS:
        return {"error": f"unsupported language: {language}"}

    digest = hashlib.sha256(script.encode()).hexdigest()[:16]

    try:
        _scan_for_mutations(script)
    except PermissionError as exc:
        logger.warning("script DENIED lang=%s sha=%s reason=%s", language, digest, exc)
        _persist_script(language, script, returncode=-1, note="denied: mutating construct")
        raise

    logger.info("script ALLOWED lang=%s sha=%s", language, digest)
    cmd = [*_INTERPRETERS[language], script]

    timeout = getattr(s, "script_timeout_seconds", 30)
    try:
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                # shell=True is NEVER used — script is passed as an arg to the interpreter
            )
        _persist_script(
            language=language,
            script=script,
            stdout=result.stdout,
            returncode=result.returncode,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        _persist_script(language, script, returncode=-1, note="timed out")
        return {"error": "script timed out"}
    except FileNotFoundError as exc:
        return {"error": f"interpreter not found: {exc}"}
    except Exception as exc:
        # TRK-024: an unexpected execution error surfaces as ToolException for
        # cross-tool consistency rather than leaking a bare exception. The read-only
        # mutation denial (PermissionError from _scan_for_mutations) is raised and
        # re-raised earlier, above this try — it never reaches here, so the read-only
        # denial is never downgraded to a generic ToolException.
        raise ToolException(f"script execution failed: {exc}") from exc
