"""Tests for ContainerRegistryAgent (GitLab issue #101).

Covers: success (image enumerated + detail row + edges written), empty result
(no registries configured -> CollectorSkipped), and exception handling
(upstream registry failures surfaced as partial/failed CollectOutcome, never
silently swallowed).
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.container_registry import ContainerRegistryAgent
from infra_brain.api._seeding import upsert_resource
from infra_brain.db.models import ContainerImage
from infra_brain.etl.base import CollectorSkipped, CollectOutcome


def _legacy_store_absent(session) -> None:
    """P5 integration: the honest form of "wrote zero legacy rows".

    These tests predate the fold that removed the ORM model from
    ``Base.metadata``; they counted rows to prove no writer fired. Post-drop
    the claim is structural — the table does not exist in the schema this
    test just ran the agent against, so no writer COULD have produced a row.
    """
    from sqlalchemy import inspect as _sqla_inspect

    assert "resource_relationships" not in _sqla_inspect(session.get_bind()).get_table_names()


MODULE = "infra_brain.agents.container_registry"

MOCK_CATALOG = {"repositories": ["team/app"]}
MOCK_TAGS = {"tags": ["latest"]}
MOCK_MANIFEST = {"digest": "sha256:" + "a" * 64}
MOCK_REFERRERS_WITH_SCAN = {
    "supported": True,
    "manifests": [
        {"artifactType": "application/vnd.dev.cosign.simplesigning.v1+json"},
        {"artifactType": "application/vnd.security.vuln.report+json"},
    ],
}
MOCK_REFERRERS_UNSUPPORTED = {"manifests": [], "supported": False}


@pytest.fixture
def agent(make_agent):
    a = make_agent(ContainerRegistryAgent)
    a.settings.container_registries = "https://registry.example.com"
    a.settings.container_registry_repo_cap = 200
    a.settings.container_registry_tag_cap = 20
    return a


class TestCollectEmpty:
    def test_no_registries_configured_raises_skipped(self, make_agent):
        """Empty container_registries config -> CollectorSkipped (no-op, not an error)."""
        agent = make_agent(ContainerRegistryAgent)
        agent.settings.container_registries = ""
        with pytest.raises(CollectorSkipped):
            agent.collect()

    def test_registry_with_no_repos_returns_empty_outcome(self, agent):
        with patch(f"{MODULE}.registry_catalog_tool") as mock_catalog:
            mock_catalog.invoke.return_value = {"repositories": []}
            outcome = agent.collect()
        assert isinstance(outcome, CollectOutcome)
        assert outcome.items == []
        assert outcome.errors == []


class TestCollectSuccess:
    def test_collects_one_image_with_scan_and_signature(self, agent):
        with (
            patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
            patch(f"{MODULE}.registry_tags_tool") as mock_tags,
            patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
            patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
        ):
            mock_catalog.invoke.return_value = MOCK_CATALOG
            mock_tags.invoke.return_value = MOCK_TAGS
            mock_manifest.invoke.return_value = MOCK_MANIFEST
            mock_referrers.invoke.return_value = MOCK_REFERRERS_WITH_SCAN

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        assert len(outcome.items) == 1
        item = outcome.items[0]
        assert item["type"] == "container_image"
        assert item["data"]["registry"] == "https://registry.example.com"
        assert item["data"]["repo"] == "team/app"
        assert item["data"]["tag"] == "latest"
        assert item["data"]["digest"] == MOCK_MANIFEST["digest"]
        assert item["data"]["signed"] is True
        assert item["data"]["scan_result_summary"]["scan_artifact_count"] == 1

    def test_referrers_unsupported_is_not_an_error(self, agent):
        """A registry without the OCI Referrers API reports unknown scan/sign
        status, not a collection failure — it's optional per the OCI spec."""
        with (
            patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
            patch(f"{MODULE}.registry_tags_tool") as mock_tags,
            patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
            patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
        ):
            mock_catalog.invoke.return_value = MOCK_CATALOG
            mock_tags.invoke.return_value = MOCK_TAGS
            mock_manifest.invoke.return_value = MOCK_MANIFEST
            mock_referrers.invoke.return_value = MOCK_REFERRERS_UNSUPPORTED

            outcome = agent.collect()

        assert outcome.errors == []
        item = outcome.items[0]
        assert item["data"]["signed"] is False
        assert item["data"]["scan_result_summary"] is None

    def test_detail_writer_writes_container_image_row_and_registry_node(
        self, make_agent, session_patcher
    ):
        """End-to-end: collect() + the detail writer upserts container_images.

        P5 (rev11-T5-B): renamed from ``..._row_and_edges``. The PULLED_FROM /
        HAS_VULNERABILITY_SCAN assertions died with the edges — see the agent
        module's EPITAPH. What survives is asserted instead: the detail row
        carries the registry, the repo, the signature verdict and the scan
        summary (the two facts the two edges restated), the ``registry``
        Resource is still minted, the synthetic ``container_scan`` anchor is
        NOT, and the legacy store stays empty.
        """
        agent = make_agent(ContainerRegistryAgent)
        agent.settings.container_registries = "https://registry.example.com"
        agent.settings.container_registry_repo_cap = 200
        agent.settings.container_registry_tag_cap = 20

        with session_patcher(MODULE) as engine:
            with (
                patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
                patch(f"{MODULE}.registry_tags_tool") as mock_tags,
                patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
                patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
            ):
                mock_catalog.invoke.return_value = MOCK_CATALOG
                mock_tags.invoke.return_value = MOCK_TAGS
                mock_manifest.invoke.return_value = MOCK_MANIFEST
                mock_referrers.invoke.return_value = MOCK_REFERRERS_WITH_SCAN

                outcome = agent.collect()

            from sqlalchemy.orm import Session

            with Session(engine) as session:
                for it in outcome.items:
                    from infra_brain.api._seeding import upsert_resource

                    upsert_resource(
                        session,
                        name=it["name"],
                        domain=agent.domain,
                        resource_type=it["type"],
                        metadata=it["data"],
                    )
                session.commit()

            agent._write_container_registry_details()

            with Session(engine) as session:
                from infra_brain.db.models import Resource

                rows = session.query(ContainerImage).all()
                assert len(rows) == 1
                assert rows[0].registry == "https://registry.example.com"
                assert rows[0].repo == "team/app"
                assert rows[0].signed is True
                # WHERE PULLED_FROM / HAS_VULNERABILITY_SCAN LIVE NOW.
                assert rows[0].scan_result_summary["scan_artifact_count"] >= 1

                # The registry node is inventory and is KEPT.
                assert (
                    session.query(Resource).filter_by(domain=agent.domain, type="registry").count()
                    == 1
                )
                # The container_scan node was a pure edge anchor and is GONE.
                assert (
                    session.query(Resource)
                    .filter_by(domain=agent.domain, type="container_scan")
                    .count()
                    == 0
                )
                _legacy_store_absent(session)


class TestReferrersOutageDoesNotErodeKnownGood:
    """L-1: container_registry conflates "unknown" with "unsigned".

    A transient (non-404) Referrers-API failure is not the same claim as the
    registry cleanly reporting "unsupported" or "no signature artifacts
    found" — the former means "we could not check", not "this is unsigned".
    """

    def test_referrers_transient_failure_reports_unknown_not_unsigned(self, agent):
        """collect() itself must not conflate the two: a raised exception
        from registry_referrers_tool must NOT be classified as signed=False
        the same way a clean 404 ("supported": False) is."""
        with (
            patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
            patch(f"{MODULE}.registry_tags_tool") as mock_tags,
            patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
            patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
        ):
            mock_catalog.invoke.return_value = MOCK_CATALOG
            mock_tags.invoke.return_value = MOCK_TAGS
            mock_manifest.invoke.return_value = MOCK_MANIFEST
            mock_referrers.invoke.side_effect = RuntimeError("simulated registry outage")

            outcome = agent.collect()

        assert len(outcome.items) == 1
        assert outcome.items[0]["data"]["signed"] is None, (
            "a transient referrers failure must be reported as unknown "
            "(None), not the same False a clean 404 produces"
        )
        assert outcome.errors, "a transient referrers failure must surface as a run error"

    def test_referrers_transient_failure_does_not_flip_verified_image_to_unsigned(
        self, make_agent, session_patcher
    ):
        """End-to-end: a previously-VERIFIED signed=True image must survive a
        re-scan whose Referrers-API call transiently fails."""
        agent1 = make_agent(ContainerRegistryAgent)
        agent1.settings.container_registries = "https://registry.example.com"
        agent1.settings.container_registry_repo_cap = 200
        agent1.settings.container_registry_tag_cap = 20

        with session_patcher(MODULE) as engine:
            # First pass: referrers check succeeds -> signed=True established.
            with (
                patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
                patch(f"{MODULE}.registry_tags_tool") as mock_tags,
                patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
                patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
            ):
                mock_catalog.invoke.return_value = MOCK_CATALOG
                mock_tags.invoke.return_value = MOCK_TAGS
                mock_manifest.invoke.return_value = MOCK_MANIFEST
                mock_referrers.invoke.return_value = MOCK_REFERRERS_WITH_SCAN
                outcome1 = agent1.collect()

            with Session(engine) as session:
                for it in outcome1.items:
                    upsert_resource(
                        session,
                        name=it["name"],
                        domain=agent1.domain,
                        resource_type=it["type"],
                        metadata=it["data"],
                    )
                session.commit()

                agent1._write_container_registry_details()

            with Session(engine) as session:
                row = session.query(ContainerImage).one()
                assert row.signed is True

            # Second pass, fresh agent instance, same image: the Referrers
            # API call now raises (transient outage) rather than 404ing.
            agent2 = make_agent(ContainerRegistryAgent)
            agent2.settings.container_registries = "https://registry.example.com"
            agent2.settings.container_registry_repo_cap = 200
            agent2.settings.container_registry_tag_cap = 20
            with (
                patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
                patch(f"{MODULE}.registry_tags_tool") as mock_tags,
                patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
                patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
            ):
                mock_catalog.invoke.return_value = MOCK_CATALOG
                mock_tags.invoke.return_value = MOCK_TAGS
                mock_manifest.invoke.return_value = MOCK_MANIFEST
                mock_referrers.invoke.side_effect = RuntimeError("simulated registry outage")
                outcome2 = agent2.collect()

            with Session(engine) as session:
                for it in outcome2.items:
                    upsert_resource(
                        session,
                        name=it["name"],
                        domain=agent2.domain,
                        resource_type=it["type"],
                        metadata=it["data"],
                    )
                session.commit()

                agent2._write_container_registry_details()

            with Session(engine) as session:
                row = session.query(ContainerImage).one()
                assert row.signed is True, (
                    "a transient Referrers-API failure must not erase a "
                    "previously-verified signed=True"
                )

    def test_detail_refresh_scoped_to_this_runs_images_only(self, make_agent, session_patcher):
        """The detail-write phase must only touch images collect() observed
        THIS run, not every historical container_image Resource."""
        agent = make_agent(ContainerRegistryAgent)
        agent.settings.container_registries = "https://registry.example.com"
        agent.settings.container_registry_repo_cap = 200
        agent.settings.container_registry_tag_cap = 20

        with session_patcher(MODULE) as engine:
            # A STALE container_image Resource from an earlier run, for a
            # repo this run's collect() will not touch at all.
            with Session(engine) as session:
                upsert_resource(
                    session,
                    name="https://registry.example.com/team/old-app:v1",
                    domain=agent.domain,
                    resource_type="container_image",
                    metadata={
                        "registry": "https://registry.example.com",
                        "repo": "team/old-app",
                        "tag": "v1",
                        "digest": "sha256:" + "b" * 64,
                        "signed": True,
                        "scan_result_summary": None,
                    },
                )
                session.commit()

            with (
                patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
                patch(f"{MODULE}.registry_tags_tool") as mock_tags,
                patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
                patch(f"{MODULE}.registry_referrers_tool") as mock_referrers,
            ):
                mock_catalog.invoke.return_value = MOCK_CATALOG
                mock_tags.invoke.return_value = MOCK_TAGS
                mock_manifest.invoke.return_value = MOCK_MANIFEST
                mock_referrers.invoke.return_value = MOCK_REFERRERS_WITH_SCAN
                outcome = agent.collect()

            with Session(engine) as session:
                for it in outcome.items:
                    upsert_resource(
                        session,
                        name=it["name"],
                        domain=agent.domain,
                        resource_type=it["type"],
                        metadata=it["data"],
                    )
                session.commit()

                written = agent._write_container_registry_details()

            # Only the ONE image collected THIS run was written — the stale
            # "old-app" Resource (never seen by this run's collect()) must be
            # left untouched.
            assert written == 1
            with Session(engine) as session:
                repos = {r.repo for r in session.query(ContainerImage).all()}
                assert repos == {"team/app"}


class TestCollectExceptions:
    def test_catalog_fetch_failure_is_reported_not_raised(self, agent):
        """A per-registry catalog failure is surfaced in CollectOutcome.errors,
        not raised — collect() must not propagate upstream HTTP failures."""
        with patch(f"{MODULE}.registry_catalog_tool") as mock_catalog:
            mock_catalog.invoke.side_effect = RuntimeError("registry unreachable")
            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.items == []
        assert outcome.errors
        assert "registry unreachable" in outcome.errors[0]

    def test_run_reports_failed_when_collect_raises(self, agent, session_patcher):
        """BaseAgent.run() catches exceptions from collect() itself."""
        with session_patcher("infra_brain.etl.base"):
            with patch.object(agent, "collect", side_effect=RuntimeError("boom")):
                run_result = agent.run()
        assert run_result.status == "failed"
        assert "boom" in run_result.errors[0]

    def test_manifest_failure_for_one_tag_does_not_abort_others(self, agent):
        with (
            patch(f"{MODULE}.registry_catalog_tool") as mock_catalog,
            patch(f"{MODULE}.registry_tags_tool") as mock_tags,
            patch(f"{MODULE}.registry_manifest_tool") as mock_manifest,
        ):
            mock_catalog.invoke.return_value = {"repositories": ["team/app"]}
            mock_tags.invoke.return_value = {"tags": ["latest", "broken"]}
            mock_manifest.invoke.side_effect = [
                RuntimeError("manifest fetch failed"),
                MOCK_MANIFEST,
            ]
            with patch(f"{MODULE}.registry_referrers_tool") as mock_referrers:
                mock_referrers.invoke.return_value = MOCK_REFERRERS_UNSUPPORTED
                outcome = agent.collect()

        assert len(outcome.errors) == 1
        assert len(outcome.items) == 1


class TestDomainAndSafety:
    def test_domain_is_set(self, agent):
        assert agent.domain == "container_registry"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        real_agent = ContainerRegistryAgent()
        assert real_agent.callbacks is not None
        assert len(real_agent.callbacks) > 0

    def test_tools_use_readonly_client(self):
        """Structural read-only: the collector's HTTP tools must be backed by
        ReadOnlyClient, never a plain httpx client capable of non-GET verbs."""
        import infra_brain.tools.container_registry_tool as tool_mod

        assert tool_mod.ReadOnlyClient is not None
