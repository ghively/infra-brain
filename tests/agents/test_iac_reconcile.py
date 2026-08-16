"""TRK-258 (3) — repair pre-F-018 same-name, different-type ``iac`` orphans.

F-018 (``_retype_existing_by_name``, see ``test_iac_retype.py``) already stops
a NEW duplicate from being created on any reclassification going forward: it
retypes the existing ``(domain, name)`` row in place before the shared upsert
runs, so a path that changes type across two collects leaves exactly one live
row from that point on.

It does nothing for rows that were ALREADY duplicated before that guard
existed -- e.g. the real finding this closes: a June ``ansible_playbook`` row
for ``iac/infra-brain/k8s/scheduler.yaml`` left live alongside a fresher
``k8s_manifest`` row for the identical path. ``IaCAgent._reconcile_stale_typed_duplicates``
is the idempotent repair pass for exactly that shape, run once per collect
inside ``_write_iac_details``.
"""

import uuid
from datetime import UTC, datetime, timedelta

from infra_brain.agents.iac import IaCAgent
from infra_brain.db.models import Resource


def _resource(session, *, name, type_, last_seen, retired_at=None, domain="iac"):
    r = Resource(
        id=uuid.uuid4(),
        domain=domain,
        type=type_,
        name=name,
        source="IaCAgent",
        zone="corporate",
        last_seen=last_seen,
        retired_at=retired_at,
        metadata_={},
    )
    session.add(r)
    session.flush()
    return r


def test_reconcile_retires_the_stale_typed_duplicate_keeping_one_live_row(
    make_agent, sqlite_engine, session_patcher
):
    """The exact TRK-258 (3) shape: a path reclassified across two collects,
    from a time before the F-018 guard existed, leaves TWO live rows for the
    same (domain, name). One reconcile pass must retire the stale one and
    leave exactly one live row -- the freshest, correctly-typed one."""
    agent = make_agent(IaCAgent)
    now = datetime.now(UTC)

    with session_patcher("infra_brain.agents.iac") as engine:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            _resource(
                s,
                name="iac/infra-brain/k8s/scheduler.yaml",
                type_="ansible_playbook",
                last_seen=now - timedelta(days=45),  # the stale June row
            )
            _resource(
                s,
                name="iac/infra-brain/k8s/scheduler.yaml",
                type_="k8s_manifest",
                last_seen=now,  # the fresh row from the most recent collect
            )
            # An unrelated name must be left completely alone.
            _resource(s, name="iac/other/unrelated.yaml", type_="k8s_manifest", last_seen=now)
            s.commit()

            retired_count = agent._reconcile_stale_typed_duplicates(s)
            s.commit()

        with Session(engine) as s:
            all_rows = (
                s.query(Resource)
                .filter_by(domain="iac", name="iac/infra-brain/k8s/scheduler.yaml")
                .all()
            )
            live_rows = [r for r in all_rows if r.retired_at is None]
            retired_rows = [r for r in all_rows if r.retired_at is not None]

    assert retired_count == 1
    # Nothing was deleted -- both rows for the path still exist in the table.
    assert len(all_rows) == 2
    # Exactly one live row remains, and it's the fresh, correctly-typed one.
    assert len(live_rows) == 1
    assert live_rows[0].type == "k8s_manifest"
    # The stale one was RETIRED (retired_at stamped), never dropped.
    assert len(retired_rows) == 1
    assert retired_rows[0].type == "ansible_playbook"

    # Unrelated name is untouched.
    with Session(engine) as s:
        other = s.query(Resource).filter_by(name="iac/other/unrelated.yaml").one()
        assert other.retired_at is None


def test_reconcile_is_idempotent(make_agent, sqlite_engine, session_patcher):
    """A second run with no live duplicates left is a clean no-op."""
    agent = make_agent(IaCAgent)
    now = datetime.now(UTC)

    with session_patcher("infra_brain.agents.iac") as engine:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            _resource(s, name="path/a.yaml", type_="ansible_playbook", last_seen=now - timedelta(days=1))
            _resource(s, name="path/a.yaml", type_="k8s_manifest", last_seen=now)
            s.commit()
            first_pass = agent._reconcile_stale_typed_duplicates(s)
            s.commit()

        with Session(engine) as s:
            second_pass = agent._reconcile_stale_typed_duplicates(s)
            s.commit()

    assert first_pass == 1
    assert second_pass == 0


def test_reconcile_never_touches_a_single_live_row_per_name(
    make_agent, sqlite_engine, session_patcher
):
    """A name with exactly one live row (the common case) is left alone."""
    agent = make_agent(IaCAgent)
    now = datetime.now(UTC)

    with session_patcher("infra_brain.agents.iac") as engine:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            _resource(s, name="path/only.yaml", type_="k8s_manifest", last_seen=now)
            s.commit()
            retired_count = agent._reconcile_stale_typed_duplicates(s)

    assert retired_count == 0


def test_reconcile_ignores_already_retired_rows_for_the_live_count(
    make_agent, sqlite_engine, session_patcher
):
    """An already-retired duplicate must not be double-counted or re-touched --
    only LIVE (retired_at IS NULL) rows compete for "most recent"."""
    agent = make_agent(IaCAgent)
    now = datetime.now(UTC)

    with session_patcher("infra_brain.agents.iac") as engine:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            _resource(
                s,
                name="path/b.yaml",
                type_="terraform_file",
                last_seen=now - timedelta(days=90),
                retired_at=now - timedelta(days=80),  # already retired long ago
            )
            _resource(s, name="path/b.yaml", type_="k8s_manifest", last_seen=now)
            s.commit()
            retired_count = agent._reconcile_stale_typed_duplicates(s)

    assert retired_count == 0
