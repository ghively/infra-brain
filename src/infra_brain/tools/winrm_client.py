"""WinRM client factory (pywinrm) — read-only-by-convention.

MR-J item 4: ``WindowsAgent._write_windows_details`` has, since it was
written, read ``getattr(self, "_winrm_client", None)`` — an instance
attribute NOTHING ever set. Every WinRM-based collector
(``_collect_services``/``_collect_software``, plus the new posture/cert/
share/local-account collectors this MR adds) is a documented no-op when the
client is ``None``, so the WinRM lane has been silently dead code since it was
introduced, independent of whether ``ANSIBLE_WIN_PASSWORD`` is configured in
the live environment (that absence is a separate, already-tracked config
item — see docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md Part 2).

This module provides the missing piece: an actual client factory. Gracefully
degrades to returning ``None`` (never raises) when ``pywinrm`` is not
installed (optional dependency — see the ``winrm`` extra in pyproject.toml)
or when credentials are not configured, exactly mirroring
``tools/vsphere.py``'s ``_PYVMOMI_AVAILABLE`` degrade-gracefully pattern.

Per docs/READONLY-MODEL.md: WinRM, like vSphere/Kubernetes/nmap, cannot be
made GET-only structurally, so it is read-only BY CONVENTION — every
PowerShell one-liner run through a client returned by this module MUST be a
read-only cmdlet (``Get-*``/``Test-*``); never ``Set-*``/``New-*``/``Remove-*``.
"""

import logging

logger = logging.getLogger(__name__)

try:
    import winrm

    _PYWINRM_AVAILABLE = True
except ImportError:
    _PYWINRM_AVAILABLE = False


def get_winrm_client(
    hostname: str,
    user: str,
    password: str,
    transport: str = "ntlm",
    port: int = 5985,
    scheme: str = "http",
    operation_timeout_sec: int = 20,
    read_timeout_sec: int = 30,
):
    """Build a ``winrm.Session`` for *hostname*, or ``None`` if unavailable.

    Never raises: returns ``None`` when ``pywinrm`` is not installed, when
    any of hostname/user/password is empty, or when session construction
    itself fails (e.g. malformed hostname). Every WinRM-based collector in
    agents/windows.py already treats a ``None`` client as "WinRM collection
    not available for this host" and no-ops cleanly.

    Bounded timeouts are mandatory here: an unreachable/unresponsive host must
    fail one host's WinRM collection quickly, not hang the whole scheduled
    WindowsAgent run (``read_timeout_sec`` must exceed ``operation_timeout_sec``
    per pywinrm's own contract).
    """
    if not _PYWINRM_AVAILABLE:
        return None
    if not hostname or not user or not password:
        return None
    endpoint = f"{scheme}://{hostname}:{port}/wsman"
    try:
        return winrm.Session(
            endpoint,
            auth=(user, password),
            transport=transport,
            operation_timeout_sec=operation_timeout_sec,
            read_timeout_sec=read_timeout_sec,
        )
    except Exception:
        logger.warning("winrm_client: failed to build a WinRM session for %s", hostname)
        return None
