"""End-of-life date tools — read-only queries to endoflife.date public API.

Also hosts the OS-string → (endoflife.date product, cycle) normalizer used by the
EOLAgent to auto-derive the EOL registry from collected OS inventory. The mapper
is a deliberately explicit regex table: an OS string we cannot confidently map is
returned as ``None`` so the caller logs + skips it rather than guessing a wrong
product/cycle (which would produce a wrong EOL date).
"""

import re

from langchain_core.tools import tool

from infra_brain.tools.http_readonly import ReadOnlyClient

_EOL_API_BASE = "https://endoflife.date/api"

# endoflife.date has renamed some product slugs since this table's slugs were
# chosen; a stale slug 301-redirects to the new one instead of 404ing, and the
# read-only client does not follow redirects (by design — see
# tools/http_readonly.py), so a stale slug here silently drops that product's
# EOL data every run (TRK-257). Map our stable internal slug (used throughout
# _OS_RULES/eol.py as the cache/lookup key) to whatever endoflife.date calls it
# today, so only this one lookup needs updating when a slug moves upstream.
_EOL_API_SLUG_ALIASES: dict[str, str] = {
    "vmware-esxi": "esxi",  # endoflife.date renamed vmware-esxi -> esxi
}


# --- OS-string → (product, cycle, friendly_label) normalizer -----------------
#
# product/cycle are the endoflife.date API slug + cycle key (e.g. "centos"/"7",
# "windows-server"/"2019"). friendly_label is the human asset_name used as the
# eol_registry upsert key. Each entry: (compiled regex, product, cycle, label).
# Order matters — more specific patterns first (e.g. 2012-R2 before 2012).
_OS_RULES: list[tuple[re.Pattern, str, str, str]] = [
    # --- Windows Server ---
    (
        re.compile(r"windows.*server.*2012\s*r2", re.I),
        "windows-server",
        "2012-R2",
        "Windows Server 2012 R2",
    ),
    (re.compile(r"windows.*server.*2012", re.I), "windows-server", "2012", "Windows Server 2012"),
    (re.compile(r"windows.*server.*2016", re.I), "windows-server", "2016", "Windows Server 2016"),
    (re.compile(r"windows.*server.*2019", re.I), "windows-server", "2019", "Windows Server 2019"),
    (re.compile(r"windows.*server.*2022", re.I), "windows-server", "2022", "Windows Server 2022"),
    # --- Windows (desktop) --- placed after Windows Server so a "Windows
    # Server 20XX" string is always claimed by the more specific rules above
    # first; these only match desktop editions (no "server" in the string).
    # `(?<!\.)` guards every bare major-version digit rule below against
    # matching a MINOR version digit instead (e.g. the trailing "9" in "8.9"
    # must never be mistaken for major version 9 — see Oracle Linux/AlmaLinux).
    (re.compile(r"windows.*(?<!\.)\b11\b", re.I), "windows", "11", "Windows 11"),
    (re.compile(r"windows.*(?<!\.)\b10\b", re.I), "windows", "10", "Windows 10"),
    # --- RHEL ---
    (re.compile(r"(red\s*hat|rhel).*(?<!\.)\b9\b", re.I), "rhel", "9", "RHEL 9"),
    (re.compile(r"(red\s*hat|rhel).*(?<!\.)\b8\b", re.I), "rhel", "8", "RHEL 8"),
    (re.compile(r"(red\s*hat|rhel).*(?<!\.)\b7\b", re.I), "rhel", "7", "RHEL 7"),
    # --- CentOS ---
    (re.compile(r"cent\s*os.*(?<!\.)\b7\b", re.I), "centos", "7", "CentOS 7"),
    (re.compile(r"cent\s*os.*(?<!\.)\b6\b", re.I), "centos", "6", "CentOS 6"),
    # --- Rocky ---
    (re.compile(r"rocky.*(?<!\.)\b9\b", re.I), "rocky-linux", "9", "Rocky Linux 9"),
    (re.compile(r"rocky.*(?<!\.)\b8\b", re.I), "rocky-linux", "8", "Rocky Linux 8"),
    # --- AlmaLinux ---
    (re.compile(r"alma.*(?<!\.)\b9\b", re.I), "almalinux", "9", "AlmaLinux 9"),
    (re.compile(r"alma.*(?<!\.)\b8\b", re.I), "almalinux", "8", "AlmaLinux 8"),
    # --- Oracle Linux ---
    (re.compile(r"oracle.*linux.*(?<!\.)\b9\b", re.I), "oracle-linux", "9", "Oracle Linux 9"),
    (re.compile(r"oracle.*linux.*(?<!\.)\b8\b", re.I), "oracle-linux", "8", "Oracle Linux 8"),
    (re.compile(r"oracle.*linux.*(?<!\.)\b7\b", re.I), "oracle-linux", "7", "Oracle Linux 7"),
    # --- SLES ---
    (re.compile(r"(sles|suse).*\b15\b", re.I), "sles", "15", "SLES 15"),
    (re.compile(r"(sles|suse).*\b11\b", re.I), "sles", "11", "SLES 11"),
    # --- Ubuntu (best-effort; capture the X.Y release as the cycle) ---
    (re.compile(r"ubuntu.*\b(\d{2}\.\d{2})\b", re.I), "ubuntu", r"\1", "Ubuntu"),
    # --- Debian --- (MIGRATION_MAP in agents/eol.py already has "debian 9"/
    # "debian 10" entries; this table was missing Debian entirely, the
    # clearest proof it was under-scoped — TRK-134).
    # Debian 13 added during the GitLab #146 re-validation (2026-08-02): the
    # live fleet's two Debian 13.6 hosts were silently skipped as unmapped
    # because this table stopped at 12 — the confirmed remnant of #146's
    # "fleet coverage gap" claim.
    (re.compile(r"debian.*(?<!\.)\b13\b", re.I), "debian", "13", "Debian 13"),
    (re.compile(r"debian.*(?<!\.)\b12\b", re.I), "debian", "12", "Debian 12"),
    (re.compile(r"debian.*(?<!\.)\b11\b", re.I), "debian", "11", "Debian 11"),
    (re.compile(r"debian.*(?<!\.)\b10\b", re.I), "debian", "10", "Debian 10"),
    (re.compile(r"debian.*(?<!\.)\b9\b", re.I), "debian", "9", "Debian 9"),
    # --- Amazon Linux ---
    (re.compile(r"amazon.*linux.*2023", re.I), "amazon-linux", "2023", "Amazon Linux 2023"),
    (re.compile(r"amazon.*linux.*(?<!\.)\b2\b", re.I), "amazon-linux", "2", "Amazon Linux 2"),
    # --- VMware ESXi ---
    (re.compile(r"esxi.*(?<!\.)\b8\b", re.I), "vmware-esxi", "8.0", "VMware ESXi 8.0"),
    (re.compile(r"esxi.*(?<!\.)\b7\b", re.I), "vmware-esxi", "7.0", "VMware ESXi 7.0"),
    (re.compile(r"esxi.*\b6\.7\b", re.I), "vmware-esxi", "6.7", "VMware ESXi 6.7"),
    (re.compile(r"esxi.*\b6\.5\b", re.I), "vmware-esxi", "6.5", "VMware ESXi 6.5"),
]


def normalize_os_string(os_string):
    """Map a raw OS string → ``(product, cycle, friendly_label)`` or ``None``.

    ``product``/``cycle`` are endoflife.date API identifiers. ``friendly_label`` is
    the eol_registry ``asset_name``. Returns ``None`` for an unmappable string so
    the caller logs + skips (never guesses). For Ubuntu the matched ``X.Y`` is
    substituted into both the cycle and the label.
    """
    if not os_string:
        return None
    text = str(os_string).strip()
    if not text:
        return None
    for pattern, product, cycle, label in _OS_RULES:
        m = pattern.search(text)
        if not m:
            continue
        # Ubuntu (and any rule using a backref) substitutes the captured version.
        if "\\" in cycle:
            ver = m.group(1)
            return product, ver, f"{label} {ver}"
        return product, cycle, label
    return None


def normalize_fingerprint(family, product, version):
    """Map Rapid7 ``osFingerprint`` fields → ``(product, cycle, friendly_label)``.

    Preferred over ``normalize_os_string`` for Rapid7 assets because the
    structured fingerprint (family="Windows", product="Windows Server 2019
    Datacenter Edition", version="1809") is far cleaner than the flat ``os``
    string. We compose a best-effort OS string from the fingerprint fields and
    reuse the existing ``_OS_RULES`` table, so all the product/cycle mappings
    (Windows Server/desktop, RHEL, CentOS, Rocky, AlmaLinux, Oracle Linux,
    SLES, Ubuntu, Debian, Amazon Linux, ESXi) stay in one place.
    Returns ``None`` for an unmappable fingerprint (caller logs + skips).
    """
    parts = [str(p).strip() for p in (family, product, version) if p and str(p).strip()]
    if not parts:
        return None
    # product usually already contains the family + edition; appending family +
    # version makes the regexes (which look for e.g. "windows server 2019",
    # "centos 7") match regardless of which field carries the version digits.
    composed = " ".join(parts)
    return normalize_os_string(composed)


@tool
def eol_cycles_tool(product: str) -> list:
    """Fetch end-of-life cycle data for a product from endoflife.date API (read-only).

    Returns a list of cycle dicts with keys: cycle, eol, latest, latestReleaseDate,
    releaseDate, lts. Raises on HTTP errors so the caller can catch per-product failures.
    """
    slug = product.lower().replace(" ", "-")
    slug = _EOL_API_SLUG_ALIASES.get(slug, slug)
    with ReadOnlyClient(timeout=15) as client:
        resp = client.get(f"{_EOL_API_BASE}/{slug}.json")
        resp.raise_for_status()
        return resp.json()
