"""Module tests for infra_brain.tools.hostmatch (MR-E TEST-4).

hostmatch.py has basic smoke coverage via tests/agents/test_inventory_reconcile.py
(a handful of assertions exercising it as a dependency); this file gives it
dedicated, exhaustive coverage of its own contract — case/whitespace/trailing
-dot normalization, FQDN vs short-name equivalence, and empty/None handling —
since it is the exact class of "two collectors describe the same host
slightly differently" logic that host identity convergence (TEST-2) depends
on across the whole reconciliation pipeline (host_reconcile.py,
inventory_reconcile.py).
"""

from infra_brain.tools.hostmatch import (
    host_domain,
    hosts_domain_conflict,
    inventory_host_index,
    is_host_in_inventory,
    normalize_host,
)


# ---------------------------------------------------------------------------
# normalize_host
# ---------------------------------------------------------------------------


def test_normalize_host_strips_fqdn_to_short_name():
    assert normalize_host("web-01.corp.example.com") == "web-01"


def test_normalize_host_lowercases():
    assert normalize_host("WEB-01") == "web-01"


def test_normalize_host_strips_trailing_dot():
    assert normalize_host("web-01.corp.example.com.") == "web-01"


def test_normalize_host_strips_surrounding_whitespace():
    assert normalize_host("  web-01  ") == "web-01"


def test_normalize_host_short_name_passthrough():
    assert normalize_host("db02") == "db02"


def test_normalize_host_empty_string_returns_empty():
    assert normalize_host("") == ""


def test_normalize_host_none_returns_empty():
    assert normalize_host(None) == ""


def test_normalize_host_combines_case_whitespace_and_fqdn():
    assert normalize_host("  Web-01.CORP.Example.COM.  ") == "web-01"


# ---------------------------------------------------------------------------
# inventory_host_index
# ---------------------------------------------------------------------------


def test_inventory_host_index_contains_both_fqdn_and_short_form():
    idx = inventory_host_index(["web-01.corp.com"])
    assert "web-01.corp.com" in idx
    assert "web-01" in idx


def test_inventory_host_index_lowercases_entries():
    idx = inventory_host_index(["WEB-01.CORP.COM"])
    assert "web-01.corp.com" in idx
    assert "web-01" in idx


def test_inventory_host_index_strips_trailing_dot():
    idx = inventory_host_index(["web-01.corp.com."])
    assert "web-01.corp.com" in idx


def test_inventory_host_index_skips_falsy_entries():
    idx = inventory_host_index(["web-01", "", None, "db-02"])
    assert idx == {"web-01", "db-02"}


def test_inventory_host_index_none_input_returns_empty_set():
    assert inventory_host_index(None) == set()


def test_inventory_host_index_empty_iterable_returns_empty_set():
    assert inventory_host_index([]) == set()


def test_inventory_host_index_accepts_any_iterable_not_just_list():
    idx = inventory_host_index(h for h in ("web-01", "db-02"))
    assert idx == {"web-01", "db-02"}


def test_inventory_host_index_dedupes_short_name_collision():
    """Two FQDNs sharing a short name both contribute it — set semantics
    just mean one copy, not an error."""
    idx = inventory_host_index(["web-01.corp.com", "web-01.dmz.example.org"])
    assert idx == {"web-01.corp.com", "web-01.dmz.example.org", "web-01"}


# ---------------------------------------------------------------------------
# is_host_in_inventory
# ---------------------------------------------------------------------------


def test_is_host_in_inventory_matches_short_name():
    idx = inventory_host_index(["web-01.corp.com", "db-02"])
    assert is_host_in_inventory("web-01", idx)


def test_is_host_in_inventory_matches_full_fqdn():
    idx = inventory_host_index(["web-01.corp.com", "db-02"])
    assert is_host_in_inventory("web-01.corp.com", idx)


def test_is_host_in_inventory_matches_discovered_fqdn_against_inventory_short_name():
    """A collector reports the FQDN but inventory only has the short form —
    matching via the short-name half of the discovered host's split."""
    idx = inventory_host_index(["db-02"])
    assert is_host_in_inventory("DB-02.anything.internal.net", idx)


def test_is_host_in_inventory_case_insensitive():
    idx = inventory_host_index(["web-01"])
    assert is_host_in_inventory("WEB-01", idx)


def test_is_host_in_inventory_false_when_absent():
    idx = inventory_host_index(["web-01", "db-02"])
    assert not is_host_in_inventory("web-99", idx)


def test_is_host_in_inventory_false_for_empty_host():
    idx = inventory_host_index(["web-01"])
    assert not is_host_in_inventory("", idx)


def test_is_host_in_inventory_false_against_empty_index():
    assert not is_host_in_inventory("web-01", set())


# ---------------------------------------------------------------------------
# host_domain (TRK-087)
# ---------------------------------------------------------------------------


def test_host_domain_qualified_returns_suffix():
    assert host_domain("web01.corp.example.com") == "corp.example.com"


def test_host_domain_qualified_lowercases_and_strips():
    assert host_domain("  WEB01.CORP.Example.COM.  ") == "corp.example.com"


def test_host_domain_single_label_returns_empty():
    assert host_domain("web01") == ""


def test_host_domain_ipv4_returns_empty():
    assert host_domain("10.0.0.5") == ""


def test_host_domain_ipv6_returns_empty():
    assert host_domain("fe80::1") == ""


def test_host_domain_empty_returns_empty():
    assert host_domain("") == ""


def test_host_domain_none_returns_empty():
    assert host_domain(None) == ""


# ---------------------------------------------------------------------------
# hosts_domain_conflict (TRK-087)
# ---------------------------------------------------------------------------


def test_hosts_domain_conflict_both_qualified_different_domains_true():
    assert hosts_domain_conflict("web01.corp.example.com", "web01.dmz.example.org") is True


def test_hosts_domain_conflict_both_qualified_same_domain_false():
    assert hosts_domain_conflict("web01.corp.example.com", "db02.corp.example.com") is False


def test_hosts_domain_conflict_one_unqualified_false():
    assert hosts_domain_conflict("web01", "web01.corp.example.com") is False
    assert hosts_domain_conflict("web01.corp.example.com", "web01") is False


def test_hosts_domain_conflict_two_short_names_false():
    assert hosts_domain_conflict("web01", "web01") is False


def test_hosts_domain_conflict_ip_side_false():
    assert hosts_domain_conflict("10.0.0.5", "web01.corp.example.com") is False


# ---------------------------------------------------------------------------
# graph_match_key — separator-insensitive host key (graph engine joins only)
# ---------------------------------------------------------------------------


def test_graph_match_key_folds_underscore_to_hyphen():
    """The homelab's two spellings of the same machine must reduce to one key.

    The services manifest says ``node-a``; the Ansible inventory that produces
    ``linux_host`` resources says ``node_a``. DNS forbids underscores in
    hostnames, so an underscore spelling is always the inventory-sanitised form
    of the hyphen one — never a genuinely different machine.
    """
    from infra_brain.tools.hostmatch import graph_match_key

    assert graph_match_key("node-a") == graph_match_key("node_a")
    assert graph_match_key("media_host") == "media-host"


def test_graph_match_key_keeps_normalize_host_semantics():
    from infra_brain.tools.hostmatch import graph_match_key

    assert graph_match_key("node_a.tailnet-example.ts.net.") == "node-a"
    assert graph_match_key("localhost") == ""
    assert graph_match_key("") == ""
    assert graph_match_key(None) == ""
    # IPs stay whole — never truncated, never separator-folded into a name.
    assert graph_match_key("10.0.0.42") == "10.0.0.42"


def test_normalize_host_itself_is_unchanged():
    """The fold deliberately lives OUTSIDE normalize_host.

    ``normalize_host`` feeds ``host_identities.short_hostname``, a PERSISTED
    key. Widening it would orphan every existing identity row and needs a
    backfill — out of scope here, so the fold is scoped to graph matching.
    """
    from infra_brain.tools.hostmatch import normalize_host

    assert normalize_host("node-a") == "node-a"
    assert normalize_host("node_a") == "node_a"
