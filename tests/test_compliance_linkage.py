"""TDD guard for Task 4.5 — link ``compliance_violations.host`` to ``resources``
via a nullable ``resource_id`` FK, plus a reconciliation backfill.

Written FIRST: must fail until ``ComplianceViolation.resource_id`` and the
reconciliation helper exist.

Phase 4 final review finding I-1: Task 4.4 made the ``Resource`` natural key
``(domain, resource_type, name)`` — one physical host legitimately owns SEVERAL
``resources`` rows with the same ``name`` across domains. The name-match
reconcile leaves ``resource_id`` NULL for these (correctly ambiguous) hosts.
The fix: rules that already hold a concrete, unambiguous ``resource_id`` must
pass it into ``ComplianceViolation(resource_id=...)`` at creation time instead
of discarding it and forcing a lossy name re-match downstream.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    ComplianceViolation,
    EolRegistry,
    Resource,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_cm(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return _get_session


def test_reconcile_links_violation_to_matching_resource(engine, session_cm):
    """A violation whose ``host`` matches exactly one resource name gets linked."""
    from infra_brain.agents.compliance import reconcile_compliance_resource_links

    with Session(engine) as s:
        resource = Resource(domain="linux", type="host", name="hostA", source="test")
        s.add(resource)
        s.flush()
        resource_id = resource.id

        violation = ComplianceViolation(
            rule="eol_overdue", host="hostA", severity="high", detail="x", status="open"
        )
        s.add(violation)
        s.commit()
        violation_id = violation.id

    with session_cm() as s:
        reconcile_compliance_resource_links(s)
        s.commit()

    with Session(engine) as s:
        linked = s.get(ComplianceViolation, violation_id)
        assert linked.resource_id == resource_id


def test_reconcile_is_orphan_free_after_run(engine, session_cm):
    """After reconciliation, no violation with a matchable host is left unlinked,
    but a violation whose host matches no resource stays correctly NULL (not
    errored)."""
    from infra_brain.agents.compliance import reconcile_compliance_resource_links

    with Session(engine) as s:
        s.add(Resource(domain="linux", type="host", name="hostA", source="test"))
        s.add(Resource(domain="linux", type="host", name="hostB", source="test"))
        s.add(
            ComplianceViolation(
                rule="eol_overdue", host="hostA", severity="high", detail="x", status="open"
            )
        )
        s.add(
            ComplianceViolation(
                rule="vuln_sla_breach", host="hostB", severity="critical", detail="y", status="open"
            )
        )
        # No matching resource for "ghost-host" — must remain unlinked, not error.
        s.add(
            ComplianceViolation(
                rule="stale_drift", host="ghost-host", severity="medium", detail="z", status="open"
            )
        )
        s.commit()

    with session_cm() as s:
        reconcile_compliance_resource_links(s)
        s.commit()

    with Session(engine) as s:
        # Orphan-detection assertion: no violation with a matchable host remains unlinked.
        resource_names = {r.name for r in s.query(Resource.name).all()}
        unlinked_matchable = (
            s.query(ComplianceViolation)
            .filter(ComplianceViolation.resource_id.is_(None))
            .filter(ComplianceViolation.host.in_(resource_names))
            .all()
        )
        assert unlinked_matchable == []

        # The genuinely-unmatchable violation stays NULL, not errored.
        ghost = s.query(ComplianceViolation).filter_by(host="ghost-host").one()
        assert ghost.resource_id is None


def test_reconcile_skips_ambiguous_name_matches(engine, session_cm):
    """If a host string matches more than one resource name, leave resource_id
    NULL rather than guessing."""
    from infra_brain.agents.compliance import reconcile_compliance_resource_links

    with Session(engine) as s:
        # Two resources sharing the same name in different domains — ambiguous.
        s.add(Resource(domain="linux", type="host", name="dup-host", source="test"))
        s.add(Resource(domain="windows", type="host", name="dup-host", source="test"))
        s.add(
            ComplianceViolation(
                rule="eol_overdue", host="dup-host", severity="high", detail="x", status="open"
            )
        )
        s.commit()

    with session_cm() as s:
        reconcile_compliance_resource_links(s)
        s.commit()

    with Session(engine) as s:
        v = s.query(ComplianceViolation).filter_by(host="dup-host").one()
        assert v.resource_id is None


def test_creation_sets_resource_id_for_multi_domain_host(engine, session_cm):
    """Cross-task regression for review finding I-1.

    A single physical host legitimately owns several ``resources`` rows with
    the SAME name across domains (r7_asset, linux_host, vsphere_vm,
    octopus_machine, windows_host — Task 4.4's natural key includes domain).
    The EOL rule (Rule 1 in ComplianceAgent.collect) holds a concrete,
    unambiguous ``EolRegistry.resource_id`` in scope — it must pass that
    resource_id into the created ``ComplianceViolation`` at creation time, NOT
    rely on the post-hoc name-match reconcile (which would find 3 matches for
    the shared name and correctly refuse to link, leaving resource_id NULL
    forever).
    """
    from infra_brain.agents.compliance import ComplianceAgent

    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        # Same name across 3 domains — the multi-domain host scenario.
        r_linux = Resource(domain="linux_host", type="host", name="multi-01", source="test")
        r_r7 = Resource(domain="r7_asset", type="host", name="multi-01", source="test")
        r_vsphere = Resource(domain="vsphere_vm", type="host", name="multi-01", source="test")
        s.add_all([r_linux, r_r7, r_vsphere])
        s.flush()
        # The EOL registry row concretely targets the linux_host resource —
        # unambiguous, even though "multi-01" as a bare name is ambiguous.
        eol_resource_id = r_linux.id
        s.add(
            EolRegistry(
                resource_id=eol_resource_id,
                asset_name="Ubuntu 18.04",
                eol_date=now - timedelta(days=10),
                pci_risk_score=8,
            )
        )
        s.commit()

    agent = ComplianceAgent.__new__(ComplianceAgent)
    agent.settings = None
    agent.callbacks = []
    agent.thresholds = {}

    with_patch = __import__("unittest.mock", fromlist=["patch"]).patch(
        "infra_brain.agents.compliance.get_session", session_cm
    )
    with with_patch:
        agent.collect()

    with Session(engine) as s:
        cv = s.query(ComplianceViolation).filter_by(rule="eol_overdue").one()
        # Name-match alone would be ambiguous (3 resources named "multi-01");
        # creation-time linkage must still resolve it correctly.
        assert cv.resource_id == eol_resource_id


def test_reconcile_does_not_overwrite_existing_resource_id(engine, session_cm):
    """Reconcile is the FALLBACK for host-only violations — it must never
    clobber a resource_id already set at creation time."""
    from infra_brain.agents.compliance import reconcile_compliance_resource_links

    with Session(engine) as s:
        correct = Resource(domain="linux_host", type="host", name="multi-02", source="test")
        wrong = Resource(domain="r7_asset", type="host", name="multi-02", source="test")
        s.add_all([correct, wrong])
        s.flush()
        correct_id = correct.id

        # Simulate a violation that was already linked at creation time to the
        # CORRECT resource, even though the bare name "multi-02" also matches
        # a second resource in another domain (would be ambiguous by name).
        s.add(
            ComplianceViolation(
                rule="eol_overdue",
                host="multi-02",
                severity="high",
                detail="x",
                status="open",
                resource_id=correct_id,
            )
        )
        s.commit()

    with session_cm() as s:
        linked = reconcile_compliance_resource_links(s)
        s.commit()

    # Reconcile only acts on resource_id IS NULL rows, so this row was never
    # touched (not eligible for accidental relinking to the wrong resource).
    assert linked == 0
    with Session(engine) as s:
        cv = s.query(ComplianceViolation).filter_by(host="multi-02").one()
        assert cv.resource_id == correct_id
