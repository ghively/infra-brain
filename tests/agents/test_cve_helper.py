"""F-008 P2 — shared CVE helper + both agents delegate to it."""

from infra_brain.agents._cve import extract_cves
from infra_brain.agents.vuln import VulnAgent
from infra_brain.agents.vuln_cve import VulnCveBridge


def test_extract_from_explicit_cves_array():
    assert extract_cves({"cves": ["cve-2026-1234", "CVE-2026-1234"]}) == ["CVE-2026-1234"]


def test_extract_from_slug_id_fallback():
    out = extract_cves({"id": "microsoft-dot_net_framework-cve-2026-23666"})
    assert out == ["CVE-2026-23666"]


def test_non_cve_check_returns_empty():
    assert extract_cves({"id": "ssl-weak-cipher", "title": "Weak cipher"}) == []


def test_both_agents_delegate_to_shared_helper():
    payload = {"id": "vendor-thing-cve-2025-9999"}
    assert VulnAgent._extract_cves(payload) == ["CVE-2025-9999"]
    assert VulnCveBridge._extract_cves(payload) == ["CVE-2025-9999"]
