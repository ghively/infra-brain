"""Tests for IdentityAgent (GitLab issue #102)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.identity import IdentityAgent
from infra_brain.db.models import (
    IdentityGroupMembership,
    IdentityPrincipal,
)
from infra_brain.etl.base import CollectorSkipped

from tests.support.pg import make_engine

MOCK_USERS = [
    {
        "id": "00u1abc",
        "status": "ACTIVE",
        "profile": {
            "login": "jdoe@example.com",
            "email": "jdoe@example.com",
            "firstName": "Jane",
            "lastName": "Doe",
        },
    },
    {
        "id": "00u2svc",
        "status": "ACTIVE",
        "profile": {
            "login": "svc-deploy@example.com",
            "email": "svc-deploy@example.com",
            "userType": "service",
        },
    },
]

MOCK_GROUPS = [
    {"id": "00g1grp", "type": "OKTA_GROUP", "profile": {"name": "infra-admins"}},
]

MOCK_GROUP_MEMBERS = {
    "00g1grp": [{"id": "00u1abc"}],
}


class TestCollectSkippedWhenUnconfigured:
    def test_collect_raises_skipped_when_unconfigured(self, make_agent):
        agent = make_agent(
            IdentityAgent, settings=SimpleNamespace(okta_url="", okta_api_token="")
        )
        with pytest.raises(CollectorSkipped):
            agent.collect("all")

    def test_collect_raises_skipped_when_token_missing(self, make_agent):
        agent = make_agent(
            IdentityAgent,
            settings=SimpleNamespace(okta_url="https://example.okta.com", okta_api_token=""),
        )
        with pytest.raises(CollectorSkipped):
            agent.collect("all")


class TestCollectSuccess:
    def test_collect_emits_principal_and_group_resources(self, make_agent):
        agent = make_agent(
            IdentityAgent,
            settings=SimpleNamespace(
                okta_url="https://example.okta.com", okta_api_token="tok123"
            ),
        )
        with (
            patch("infra_brain.agents.identity.okta_users_tool") as mock_users,
            patch("infra_brain.agents.identity.okta_groups_tool") as mock_groups,
            patch("infra_brain.agents.identity.okta_group_members_tool") as mock_members,
        ):
            mock_users.invoke.return_value = MOCK_USERS
            mock_groups.invoke.return_value = MOCK_GROUPS
            mock_members.invoke.side_effect = lambda args, config=None: MOCK_GROUP_MEMBERS.get(
                args["group_id"], []
            )

            outcome = agent.collect(scope="all")

        assert outcome.errors == []
        principal_items = [i for i in outcome.items if i["type"] == "identity_principal"]
        group_items = [i for i in outcome.items if i["type"] == "identity_group"]
        assert len(principal_items) == 2
        assert len(group_items) == 1
        assert principal_items[0]["name"] == "jdoe@example.com"
        assert group_items[0]["data"]["member_count"] == 1

    def test_collect_returns_empty_when_okta_has_no_users_or_groups(self, make_agent):
        agent = make_agent(
            IdentityAgent,
            settings=SimpleNamespace(
                okta_url="https://example.okta.com", okta_api_token="tok123"
            ),
        )
        with (
            patch("infra_brain.agents.identity.okta_users_tool") as mock_users,
            patch("infra_brain.agents.identity.okta_groups_tool") as mock_groups,
            patch("infra_brain.agents.identity.okta_group_members_tool") as mock_members,
        ):
            mock_users.invoke.return_value = []
            mock_groups.invoke.return_value = []
            mock_members.invoke.return_value = []

            outcome = agent.collect(scope="all")

        assert outcome.items == []
        assert outcome.errors == []

    def test_collect_appends_error_on_users_fetch_failure_but_does_not_raise(self, make_agent):
        """Partial failure: users fetch blows up, groups still collected — errors
        surfaced on the outcome (partial), not swallowed and not fatal."""
        agent = make_agent(
            IdentityAgent,
            settings=SimpleNamespace(
                okta_url="https://example.okta.com", okta_api_token="tok123"
            ),
        )
        with (
            patch("infra_brain.agents.identity.okta_users_tool") as mock_users,
            patch("infra_brain.agents.identity.okta_groups_tool") as mock_groups,
            patch("infra_brain.agents.identity.okta_group_members_tool") as mock_members,
        ):
            mock_users.invoke.side_effect = RuntimeError("okta 500")
            mock_groups.invoke.return_value = MOCK_GROUPS
            mock_members.invoke.return_value = []

            outcome = agent.collect(scope="all")

        assert any("users failed" in e for e in outcome.errors)
        assert any(i["type"] == "identity_group" for i in outcome.items)


class TestDetailWritePhase:
    @pytest.fixture
    def sqlite_session_factory(self):
        engine = make_engine()
        return engine

    def test_write_identity_details_upserts_principals_and_memberships(
        self, make_agent, sqlite_session_factory, monkeypatch
    ):
        agent = make_agent(IdentityAgent)
        agent._last_users = MOCK_USERS
        agent._last_groups = MOCK_GROUPS
        agent._last_memberships = MOCK_GROUP_MEMBERS

        engine = sqlite_session_factory

        def _fake_get_session():
            class _Ctx:
                def __enter__(self_inner):
                    self_inner.session = Session(engine)
                    return self_inner.session

                def __exit__(self_inner, *exc):
                    self_inner.session.close()
                    return False

            return _Ctx()

        monkeypatch.setattr("infra_brain.agents.identity.get_session", _fake_get_session)

        n = agent._write_identity_details()

        assert n >= 3  # 2 principals + 1 membership row
        with Session(engine) as session:
            principals = session.query(IdentityPrincipal).all()
            memberships = session.query(IdentityGroupMembership).all()
            assert len(principals) == 2
            assert len(memberships) == 1
            assert memberships[0].group_name == "infra-admins"

    def test_write_identity_details_empty_is_noop(self, make_agent, sqlite_session_factory, monkeypatch):
        agent = make_agent(IdentityAgent)
        agent._last_users = []
        agent._last_groups = []
        agent._last_memberships = {}

        engine = sqlite_session_factory

        def _fake_get_session():
            class _Ctx:
                def __enter__(self_inner):
                    self_inner.session = Session(engine)
                    return self_inner.session

                def __exit__(self_inner, *exc):
                    self_inner.session.close()
                    return False

            return _Ctx()

        monkeypatch.setattr("infra_brain.agents.identity.get_session", _fake_get_session)

        n = agent._write_identity_details()
        assert n == 0

    # ── The two IS_PRINCIPAL_FOR tests that stood here — DELETED (P5). ──────
    #
    # ``test_emit_is_principal_for_edges_matches_existing_convergence_node``
    # and ``..._noop_when_no_convergence_nodes`` tested
    # ``IdentityAgent._emit_is_principal_for_edges``, which P5 deleted along
    # with the ``resource_relationships`` store it was the only writer into.
    # Both mocked ``emit_edges_batch`` and asserted on the edge dicts handed to
    # it, so there is nothing left for either to observe — no behaviour of this
    # agent changed, a method was removed.
    #
    # They are NOT ported to ``graph_edges``: unlike the iac migrations, this
    # edge did not move, it went away (retired collector, zero rows ever — see
    # the epitaph in agents/identity.py). A port would be a test of a
    # declaration nobody wrote. If Okta is ever wired up and the edge returns
    # as a ``spec.emits_edges`` declaration, its test comes back with it, over
    # ``identity_principals`` rather than over a mocked emit call.
    #
    # The surviving detail-write coverage above (``_write_identity_details``
    # into ``identity_principals`` / ``identity_group_memberships``) is
    # untouched and still green — that is this agent's actual output.


class TestDomain:
    def test_domain_is_set(self, make_agent):
        agent = make_agent(IdentityAgent)
        assert agent.domain == "identity"

    def test_callbacks_wired(self, make_agent):
        agent = make_agent(IdentityAgent, callbacks=[MagicMock()])
        assert agent.callbacks is not None
        assert len(agent.callbacks) > 0
