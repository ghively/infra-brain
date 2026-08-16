"""Multi-collector convergence tests (MR-E TEST-2).

MR-B fixed a severity-vocabulary case mismatch (a writer stored ``"Critical"``
while a reader filtered on ``"critical"`` — silent zero rows) and MR-C fixed an
edge-direction inversion in the knowledge graph. Neither bug was caught by the
existing per-agent test suites, because each agent's tests only ever check
that agent's OWN writes in isolation — nothing asserted that two independent
writers touching the *same* underlying data actually agree with each other.

This module seeds one shared in-memory SQLite database and runs pairs of real
agent writer methods (not reimplemented logic) against the overlapping data,
asserting they converge:

  1. Severity: VulnAgent writes the canonical VulnQueueItem.severity value;
     ComplianceAgent independently re-derives a severity band from that same
     row (via ``normalize_severity``) for its SLA-breach rule. Both must
     agree on "critical" — if either used a raw, un-normalized string
     comparison (the MR-B bug class), they would silently diverge.

  2. Edge direction — RETIRED IN P5, see the section epitaph below. It paired
     VulnAgent._write_graph_edges against
     GraphMaintenanceAgent._populate_typed_relationships on VULNERABLE_TO (the
     MR-C bug class: two writers disagreeing on edge direction). P5 deleted the
     VulnAgent writer along with the resource_relationships store, leaving one
     writer and therefore nothing to converge.

  3. Resource identity: HostReconcileAgent._build_merged_hosts() merges
     per-domain sightings of "the same host" (Rapid7 asset, Linux inventory,
     vSphere VM, ...) into one canonical record keyed by
     tools/hostmatch.py's normalize_host(). A Linux collector reporting the
     short hostname and a Rapid7 asset reporting the FQDN for the SAME
     physical host must converge on ONE HostIdentity row carrying both
     source resource ids — not two separate identities for one machine.

  4. TRK-062 cross-hostname IP correlation: the converse of #3 — two
     sightings that share an IP address but report DIFFERENT hostnames (no
     shared DNS label) used to produce two disconnected HostIdentity rows with
     no link between them. Since P5 the surviving mechanism is
     ``graph_phase3.resolve_entities``, which promotes such a pair into the
     REVIEW QUEUE (never an edge, never a merge) when the shared address is
     routable and claimed by exactly those two hosts. The tests prove the
     positive case and all three guards (different IP, 3+-host ambiguous IP,
     non-routable IP). See docs/TRACKER.md TRK-062 and section 4's own header.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.compliance import ComplianceAgent
from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.agents.vuln import VulnAgent
from infra_brain.db.models import (
    ComplianceViolation,
    HostIdentity,
    LinuxHost,
    R7Asset,
    Resource,
    VsphereVm,
    VulnQueueItem,
)
from infra_brain.db.severity import CRITICAL

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


# ``_sqlite_emit_edges_batch`` (the INSERT-OR-IGNORE shim that mimicked the
# legacy upsert for SQLite) was deleted at the P5 integration with its last
# caller — there is no ``resource_relationships`` table to shim writes into.


def _vuln_agent():
    agent = VulnAgent.__new__(VulnAgent)
    agent.settings = None
    agent.callbacks = []
    return agent


def _compliance_agent():
    agent = ComplianceAgent.__new__(ComplianceAgent)
    agent.settings = None
    agent.callbacks = []
    agent.thresholds = {"vuln_sla_days": 30, "stale_drift_days": 14}
    return agent


# ``_graph_maintenance_agent`` was DELETED here (P5, rev11-T5-B): it built an
# agent solely to drive ``_populate_typed_relationships``, which no longer
# exists.


# ---------------------------------------------------------------------------
# 1. Severity convergence: VulnAgent writer vs ComplianceAgent's independent
#    re-derivation of the same finding's severity band.
# ---------------------------------------------------------------------------


def test_vuln_and_compliance_agents_converge_on_critical_severity(engine):
    """Seed a VulnQueueItem the way VulnAgent's own upsert would (canonical
    lowercase severity, via _upsert_vuln_row), past its SLA. Then run
    ComplianceAgent.collect() against the SAME data and assert its
    independently-derived violation severity is ALSO "critical" — not "high"
    or "Critical", which is exactly what a case-mismatched string comparison
    (the MR-B bug class) would silently produce.
    """
    now = datetime.now(timezone.utc)
    resource_id = None
    with Session(engine) as s:
        r = Resource(domain="vuln", type="r7_asset", name="crit-host-01", source="rapid7")
        s.add(r)
        s.flush()
        resource_id = r.id
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    # Writer #1: VulnAgent's real upsert path, fed a mixed-case raw severity
    # exactly as Rapid7's API would send it ("Critical", not "critical").
    vuln_agent = _vuln_agent()
    with patch("infra_brain.agents.vuln.get_session", _get_session):
        with Session(engine) as s:
            vuln_agent._upsert_vuln_row(
                s,
                resource_id,
                "CVE-2026-9001",
                vuln_agent._severity_band("Critical", None),
                # first_seen (GitLab #136): 10 days ago with a 3-day critical
                # window -> sla_due is already 7 days past.
                now - timedelta(days=10),
                now,
            )
            s.commit()

    with Session(engine) as s:
        row = s.query(VulnQueueItem).one()
        assert row.severity == CRITICAL, (
            f"VulnAgent must store the canonical lowercase severity, got {row.severity!r}"
        )

    # Writer #2: ComplianceAgent's real rule evaluation + upsert, run against
    # the SAME VulnQueueItem row.
    compliance_agent = _compliance_agent()
    with patch("infra_brain.agents.compliance.get_session", _get_session):
        compliance_agent.collect()

    with Session(engine) as s:
        violations = (
            s.query(ComplianceViolation).filter(ComplianceViolation.rule == "vuln_sla_breach").all()
        )
    assert len(violations) == 1, "ComplianceAgent must raise exactly one vuln_sla_breach violation"
    assert violations[0].severity == CRITICAL, (
        "ComplianceAgent's independently-derived severity must converge with "
        f"VulnAgent's canonical 'critical' -- got {violations[0].severity!r}. A "
        "case-mismatched comparison (e.g. v.severity == 'Critical') would "
        "silently fall through to 'high' here."
    )


def test_vuln_and_compliance_agents_agree_high_is_not_critical(engine):
    """Converse case: a genuinely "high" (not critical) severity finding must
    NOT be promoted to "critical" by either writer -- proves the convergence
    assertion isn't just trivially true for one hardcoded band."""
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="vuln", type="r7_asset", name="high-host-01", source="rapid7")
        s.add(r)
        s.flush()
        resource_id = r.id
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    vuln_agent = _vuln_agent()
    with patch("infra_brain.agents.vuln.get_session", _get_session):
        with Session(engine) as s:
            vuln_agent._upsert_vuln_row(
                s,
                resource_id,
                "CVE-2026-9002",
                vuln_agent._severity_band("High", None),
                # first_seen (GitLab #136): 10 days ago with a 7-day high
                # window -> sla_due is already 3 days past.
                now - timedelta(days=10),
                now,
            )
            s.commit()

    with Session(engine) as s:
        row = s.query(VulnQueueItem).one()
        assert row.severity == "high"

    compliance_agent = _compliance_agent()
    with patch("infra_brain.agents.compliance.get_session", _get_session):
        compliance_agent.collect()

    with Session(engine) as s:
        violation = (
            s.query(ComplianceViolation).filter(ComplianceViolation.rule == "vuln_sla_breach").one()
        )
    assert violation.severity == "high"


# ---------------------------------------------------------------------------
# 2. Edge direction/type convergence — RETIRED IN P5. There is only one writer
#    now, so there is nothing left to converge.
# ---------------------------------------------------------------------------
#
# Two tests stood here:
#   ``test_vuln_agent_and_graph_maintenance_converge_on_vulnerable_to_direction``
#   ``test_graph_maintenance_alone_reproduces_same_edge_vuln_agent_wrote``
#
# They seeded one (VulnQueueItem, R7VulnCve, R7Vulnerability) chain and ran BOTH
# independent VULNERABLE_TO writers against it — in both orderings, so that
# convergence could not be an artifact of write order — asserting the two agreed
# on relationship_type and on direction (asset -> vulnerability) and collapsed
# onto a SINGLE row rather than a direction-inverted duplicate. That is the MR-C
# bug class this module was built to catch.
#
# P5 deleted BOTH writers: ``VulnAgent._write_graph_edges`` (epitaph in
# agents/vuln.py: Rapid7 is not deployed, the collector is retired, zero live
# rows ever) and ``GraphMaintenanceAgent._populate_typed_relationships``
# (see tests/agents/test_graph_maintenance_legacy_writers_gone.py), along with
# the ``resource_relationships`` store they wrote into. No VULNERABLE_TO
# emitter of any kind survives, so the direction/convergence hazard is
# structurally absent, not merely untested. See also
# tests/agents/test_vuln_edge_props_unified.py, whose entire TRK-088 subject
# (the two writers' PROPERTY shapes agreeing under a full-overwrite upsert)
# died for exactly the same reason on the same day.
#
# The other three convergence sections in this module are untouched and still
# green: they do not involve the deleted writer.


# ---------------------------------------------------------------------------
# 3. Resource identity convergence: HostReconcileAgent merging per-domain
#    sightings (Linux short hostname vs Rapid7 FQDN) of the same physical
#    host into one canonical HostIdentity.
# ---------------------------------------------------------------------------


def _host_reconcile_agent():
    agent = HostReconcileAgent.__new__(HostReconcileAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def test_linux_and_rapid7_sightings_of_the_same_host_converge_on_one_identity(engine):
    """LinuxAgent reports a bare short hostname ("conv-web-01"); the Rapid7
    collector reports the FQDN for the SAME machine ("conv-web-01.corp.example.com").
    HostReconcileAgent must normalize both through hostmatch.normalize_host()
    and merge them into ONE record carrying both source resource ids -- not
    two separate host identities for what is actually one machine."""
    linux_resource_id = uuid.uuid4()

    with Session(engine) as s:
        linux_res = Resource(
            id=linux_resource_id,
            domain="linux",
            type="host",
            name="conv-web-01",  # short hostname, as LinuxAgent reports it
            source="LinuxAgent",
        )
        s.add(linux_res)
        s.flush()
        s.add(
            LinuxHost(
                resource_id=linux_resource_id,
                distro="Ubuntu 22.04",
                kernel="5.15.0",
                arch="x86_64",
            )
        )
        r7_res = Resource(domain="vuln", type="r7_asset", name="conv-web-01-r7", source="rapid7")
        s.add(r7_res)
        s.flush()
        s.add(
            R7Asset(
                resource_id=r7_res.id,
                r7_asset_id=51001,
                # FQDN, as the Rapid7 collector reports it -- must normalize
                # to the same short form ("conv-web-01") as the Linux side.
                hostname="conv-web-01.corp.example.com",
                ip="10.5.5.5",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _host_reconcile_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", _get_session):
        merged, ip_conflicts, _identity_conflicts = agent._build_merged_hosts()
        assert "conv-web-01" in merged, (
            f"Linux short hostname and Rapid7 FQDN must normalize to the same "
            f"key -- got merged keys: {list(merged.keys())}"
        )
        record = merged["conv-web-01"]
        assert record.get("linux_resource_id") == linux_resource_id
        assert record.get("r7_resource_id") is not None

        n_new, n_updated = agent._upsert_identities(merged)

    assert n_new == 1, "the two sightings must converge on exactly ONE new HostIdentity"

    with Session(engine) as s:
        identities = s.query(HostIdentity).filter_by(short_hostname="conv-web-01").all()
    assert len(identities) == 1, (
        f"expected one canonical identity for the host, found {len(identities)} -- "
        "a resource-identity convergence failure would show up as duplicate "
        "HostIdentity rows for the same physical machine"
    )
    identity = identities[0]
    assert identity.linux_resource_id == linux_resource_id
    assert identity.r7_resource_id is not None


# ---------------------------------------------------------------------------
# 4. TRK-062: cross-hostname IP correlation — a QUESTION, not an edge.
#
#    BEFORE (the gap, post-27dd424 through 2026-07-22): HostReconcileAgent keyed
#    purely on normalize_host(), so two hostname-BEARING sources reporting the
#    SAME machine under DIFFERENT names (vSphere "vm-web-77" + Rapid7
#    "scan-web-77" on one IP) landed in two dict keys, produced 2 HostIdentity
#    rows, and were never compared by IP at all.
#
#    THEN (2026-07-22): _emit_cross_hostname_ip_edges restored the IP-only
#    capability as a legacy IS_SAME_AS edge at the weakest tier (0.70), with
#    three guards — exactly-two-hosts-share-the-IP, routable addresses only, and
#    never a merge.
#
#    NOW (P5, 2026-08-12): that writer is deleted and the capability lives in
#    graph_phase3.resolve_entities as a REVIEW PROMOTION rather than an
#    assertion. A shared correlatable IP with no name support floors the pair at
#    FUZZY_REVIEW_MIN, which puts the question in front of a human and can never
#    auto-emit. The guards did not die with the pass that owned them: they are
#    ported into _correlatable_ips / SharedIpIndex, because a review queue full
#    of one NAT gateway paired against everything behind it is not a safer
#    failure than a wrong edge — it is a queue nobody reads.
#
#    The tests below are the same four scenarios re-pointed at the surviving
#    mechanism: the positive case now QUEUES (and still does not merge), and the
#    three guards still refuse. Same source rows, same convergence question, one
#    writer instead of two. See docs/TRACKER.md TRK-062.
# ---------------------------------------------------------------------------


def _resolve(engine):
    """Materialise host nodes from the seeded source tables, then resolve.

    Deliberately drives ``materialize_host_nodes`` rather than hand-building
    nodes: this is a CONVERGENCE test, so the input must be the same VsphereVm /
    R7Asset rows the collectors write, not a fixture that has already done the
    resolver's job for it.
    """
    from infra_brain import graph_phase3

    with Session(engine) as s:
        graph_phase3.materialize_host_nodes(s)
        s.commit()
        counts = graph_phase3.resolve_entities(s, materialize=False)
        s.commit()
    return counts


def _review_candidates(engine):
    from infra_brain.db.models import ProposedAction
    from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type=REVIEW_ACTION_TYPE).all()
        return [c for row in rows for c in (row.payload or {}).get("candidate_matches", [])]


def _legacy_store_absent(engine):
    """P5 replacement for querying legacy IS_SAME_AS rows: the table is gone.

    ``create_all`` no longer builds ``resource_relationships`` (the model left
    ``Base.metadata`` with the drop), so "nothing writes the legacy store" is
    now structural — there is no store to write.
    """
    from sqlalchemy import inspect as _sqla_inspect

    return "resource_relationships" not in _sqla_inspect(engine).get_table_names()


def _seed_vm(session, name, ip, *, moref):
    res = Resource(domain="vsphere", type="vsphere_vm", name=name, source="vsphere")
    session.add(res)
    session.flush()
    session.add(
        VsphereVm(
            resource_id=res.id,
            vcenter="vc01",
            moref=moref,
            name=name,
            guest_hostname=name,
            ip_address=ip,
        )
    )
    return res.id


def _seed_r7(session, name, ip, *, asset_id):
    res = Resource(domain="vuln", type="r7_asset", name=name, source="rapid7")
    session.add(res)
    session.flush()
    session.add(R7Asset(resource_id=res.id, r7_asset_id=asset_id, hostname=name, ip=ip))
    return res.id


def test_ip_only_cross_source_match_is_queued_for_review_not_merged(engine):
    """The TRK-062 positive case, under the resolver.

    A vSphere VM ("vm-web-77") and a Rapid7 asset ("scan-web-77") share one
    routable IP and share no DNS label, so ``normalize_host`` cannot merge them
    and name similarity alone leaves them far below the review floor. The pair
    now reaches the REVIEW QUEUE — the capability the deleted 0.70 tier
    provided, relocated from an assertion to a question — while still producing
    NO edge in either store and NO merge of the two identities.
    """
    with Session(engine) as s:
        _seed_vm(s, "vm-web-77", "10.20.30.40", moref="vm-77")
        _seed_r7(s, "scan-web-77", "10.20.30.40", asset_id=61001)
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _host_reconcile_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", _get_session):
        merged, _ip_conflicts, _identity_conflicts = agent._build_merged_hosts()
        # NOT A MERGE, part 1: the two sightings still land under DIFFERENT keys.
        # Bare-IP equality is too weak to merge identities on (TRK-087), and that
        # judgement is unchanged by who does the correlating.
        assert merged["vm-web-77"].get("r7_resource_id") is None
        assert merged["scan-web-77"].get("vsphere_resource_id") is None
        n_new, _n_updated = agent._upsert_identities(merged)

    assert n_new == 2, "still two HostIdentity rows — a question, never a merge"

    counts = _resolve(engine)

    assert counts["nodes_considered"] == 2
    assert counts["deterministic_edges"] == 0
    assert counts["probabilistic_edges"] == 0, "a shared IP can never auto-emit"
    assert counts["review_queued"] == 1, (
        "the pair the deleted 0.70 pass asserted must now reach a human, not be dropped"
    )
    assert _legacy_store_absent(engine), "the legacy store must not even exist"

    (candidate,) = _review_candidates(engine)
    assert candidate["evidence"]["ip_overlap"] is True
    assert "ip_overlap_ambiguous" not in candidate["evidence"], "one routable IP, two claimants"
    # These names score 0.62 alone — below the review floor, so without the
    # shared IP this pair is dropped, which is exactly the GAP-2 behaviour. The
    # queued score must sit inside the review band: high enough to ask, never
    # high enough to assert.
    from infra_brain import graph_phase3

    assert graph_phase3._score_pair("vm-web-77", "scan-web-77") < graph_phase3.FUZZY_REVIEW_MIN
    assert graph_phase3.FUZZY_REVIEW_MIN <= candidate["score"] < graph_phase3.FUZZY_AUTO_EMIT_MIN

    with Session(engine) as s:
        identities = s.query(HostIdentity).all()
        assert {i.short_hostname for i in identities} == {"vm-web-77", "scan-web-77"}
        assert all(i.r7_resource_id is None or i.vsphere_resource_id is None for i in identities)


def test_different_ip_hosts_do_not_correlate(engine):
    """Negative control: correlation keys on a genuinely shared IP, not on
    "both exist". Different hostnames AND different IPs must produce silence —
    not an edge, and not a question either."""
    with Session(engine) as s:
        _seed_vm(s, "vm-unrelated-a", "10.20.30.40", moref="vm-a")
        _seed_r7(s, "scan-unrelated-b", "10.20.30.99", asset_id=61002)
        s.commit()

    counts = _resolve(engine)

    assert counts["review_queued"] == 0
    assert counts["deterministic_edges"] == 0
    assert counts["probabilistic_edges"] == 0
    assert _review_candidates(engine) == []
    assert _legacy_store_absent(engine)


def test_ambiguous_shared_ip_across_three_hosts_is_not_correlated(engine):
    """The 3+-claimant guard, ported from the deleted pass into SharedIpIndex.

    An IP three or more distinct hosts report is a NAT gateway, a reused DHCP
    lease or a shared VIP — not one machine seen thrice. The old pass refused
    such an address outright; the resolver withholds the review FLOOR, so the
    pair falls back to what its names justify (nothing, here). Promoting it
    would fan out C(3,2) questions from one address, and C(n,2) on a real
    estate.
    """
    with Session(engine) as s:
        _seed_r7(s, "nat-host-a", "10.9.9.9", asset_id=62001)
        _seed_r7(s, "nat-host-b", "10.9.9.9", asset_id=62002)
        _seed_vm(s, "nat-host-c", "10.9.9.9", moref="vm-c")
        s.commit()

    counts = _resolve(engine)

    assert counts["nodes_considered"] == 3
    assert counts["review_queued"] == 0, (
        "an IP shared by 3+ hosts is ambiguous (NAT/DHCP reuse) — asking about "
        "every pair of them is noise, not diligence"
    )
    assert counts["probabilistic_edges"] == 0
    assert _review_candidates(engine) == []


def test_loopback_shared_ip_is_not_correlated(engine):
    """The routability guard, ported from the deleted ``_is_correlatable_ip``.

    Every host reports its own 127.0.0.1. Treating that as a shared address
    would pair every machine in the estate with every other — the single
    loudest way to make the review queue useless.
    """
    with Session(engine) as s:
        _seed_vm(s, "lo-host-a", "127.0.0.1", moref="vm-lo-a")
        _seed_r7(s, "lo-host-b", "127.0.0.1", asset_id=63001)
        s.commit()

    counts = _resolve(engine)

    assert counts["review_queued"] == 0, "loopback must never promote a pair to review"
    assert counts["probabilistic_edges"] == 0
    assert _review_candidates(engine) == []
