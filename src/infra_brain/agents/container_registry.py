"""ContainerRegistryAgent — registry-side container image scanning (GitLab issue #101).

Ranked #8 of 13 roadmap domain-agent candidates — the one candidate with a
pre-existing reserved graph slot: ``db/relationships.py``'s ``RelationshipType``
enum already carried ``HOSTED_BY`` marked ``[DEFERRED — no container
collector]``. Per the issue's own adversarial-review correction, THIS agent
does NOT activate that edge — registry-side image scanning tells us nothing
about where (or whether) an image is currently running, which needs a
separate running-container collector still blocked on TRK-041/K8s.

EPITAPH — this agent no longer emits ANY edge (P5, rev11-T5-B). It used to
write two into ``resource_relationships``, the store P5 removes:

  * ``PULLED_FROM`` (container_image -> registry). WHERE THE FACT LIVES NOW:
    ``container_images.registry``, the column written three statements above
    the deleted edge build and the same string the edge's target Resource was
    keyed by. The ``container_registry/registry`` Resource row is KEPT — a
    registry is a real, independently-referrable entity that belongs in the
    inventory, and a future ``NodeSpec`` over it would need the population to
    exist (the ``ContainerImage`` failure mode iac's adoption exists to avoid).
    Only the edge went.
  * ``HAS_VULNERABILITY_SCAN`` (container_image -> container_scan). The
    ``container_scan`` node went WITH it, unlike the registry: its name was the
    synthetic ``f"{image}::scan"``, its metadata was a byte-for-byte restatement
    of ``container_images.scan_result_summary`` written immediately above, and a
    grep of ``src/`` finds no reader of the type outside this file. It was an
    edge anchor and nothing else. WHERE THE FACT LIVES NOW:
    ``container_images.scan_result_summary`` + ``.signed``.

``RUNS_IMAGE`` — the one relationship an image participates in that survives —
is not this agent's: it is DECLARED on iac's ``AgentSpec`` (compose file ->
ContainerImage) and materialised into ``graph_edges`` by ``graph_engine``.

Read-only via ``tools/container_registry_tool.py``, which only ever issues
GET requests against the OCI Distribution Specification v2 API (structural
read-only, ``ReadOnlyClient`` — see ``docs/READONLY-MODEL.md``). Deliberately
registry-agnostic (Docker Hub / GitLab Container Registry / Harbor / ECR /
GHCR / ACR all implement the same endpoints) per the issue's note about the
open GitLab-instance-migration question — nothing here is GitLab-specific.
"""

import logging
from datetime import timedelta

from infra_brain.db.models import ContainerImage
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector, ReconcileScope
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.container_registry_tool import (
    registry_catalog_tool,
    registry_manifest_tool,
    registry_referrers_tool,
    registry_tags_tool,
)

logger = logging.getLogger(__name__)

# OCI referrer ``artifactType`` prefixes this agent recognizes as a signature
# (cosign's default OCI-stored signature convention) vs. a vulnerability-scan
# attestation. Best-effort: any scanner/signer not matching either prefix is
# still counted in the summary but does not flip ``signed``.
_SIGNATURE_ARTIFACT_PREFIXES = ("application/vnd.dev.cosign", "application/vnd.dev.sigstore")
_SCAN_ARTIFACT_PREFIXES = (
    "application/vnd.security.vuln",
    "application/vnd.aquasec.trivy",
    "application/sarif+json",
    "application/vnd.cyclonedx",
    "application/spdx+json",
)


class ContainerRegistryAgent(ETLConnector):
    """Enumerates images across configured registries and scans referrers for signing/vuln artifacts."""

    spec = AgentSpec(
        domain="container_registry",
        tier=Tier.COLLECTOR,
        schedule="15 4 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        registries = [
            r.strip() for r in (self.settings.container_registries or "").split(",") if r.strip()
        ]
        if not registries:
            raise CollectorSkipped("no container_registries configured")

        repo_cap = self.settings.container_registry_repo_cap
        tag_cap = self.settings.container_registry_tag_cap
        _cb = {"callbacks": self.callbacks}

        items: list[dict] = []
        errors: list[str] = []
        # L-1: a transient (non-404) Referrers-API failure is NOT the same
        # claim as the registry cleanly reporting "unsupported" (a 404,
        # handled inside registry_referrers_tool itself and returned as a
        # normal {"supported": False} dict, never raised). Track per-image
        # referrers-check success/failure through the same shared primitive
        # destructive/security-downgrade passes use elsewhere, so the write
        # phase can tell "we could not check this run" apart from "we
        # checked and got a conclusive answer" — see `_collect_one_image`.
        referrers_scope = ReconcileScope(label="referrers check")

        for registry_url in registries:
            try:
                catalog = registry_catalog_tool.invoke(
                    {"registry_url": registry_url, "n": repo_cap}, config=_cb
                )
            except Exception as exc:
                errors.append(f"{registry_url}: catalog fetch failed: {exc}")
                continue

            repos = (catalog or {}).get("repositories") or []
            for repo in repos[:repo_cap]:
                try:
                    tags_resp = registry_tags_tool.invoke(
                        {"registry_url": registry_url, "repo": repo}, config=_cb
                    )
                except Exception as exc:
                    errors.append(f"{registry_url}/{repo}: tags fetch failed: {exc}")
                    continue

                tags = (tags_resp or {}).get("tags") or []
                for tag in tags[:tag_cap]:
                    item = self._collect_one_image(
                        registry_url, repo, tag, _cb, errors, referrers_scope
                    )
                    if item is not None:
                        items.append(item)

        # L-1: scope the detail-write phase to exactly the images observed
        # THIS run — consumed by `_write_container_registry_details` so a
        # re-scan never touches a container_image Resource this run's
        # collect() didn't even look at.
        self._collected_image_names = {it["name"] for it in items}

        all_errors = errors + referrers_scope.errors
        if all_errors and not items:
            return CollectOutcome(items=[], errors=all_errors)
        return CollectOutcome(items=items, errors=all_errors)

    def _collect_one_image(
        self, registry_url, repo, tag, _cb, errors: list[str], referrers_scope: ReconcileScope
    ) -> dict | None:
        try:
            manifest = registry_manifest_tool.invoke(
                {"registry_url": registry_url, "repo": repo, "tag": tag}, config=_cb
            )
        except Exception as exc:
            errors.append(f"{registry_url}/{repo}:{tag}: manifest fetch failed: {exc}")
            return None

        digest = (manifest or {}).get("digest", "")
        # L-1: None means "not verified this run" — distinct from the
        # concrete True/False a CONCLUSIVE referrers check produces (whether
        # that check found signature/scan artifacts, found none, or hit a
        # clean "unsupported" 404 — all three are conclusive, successful
        # calls). Only a successful call may set a concrete value here; the
        # write phase must never treat "unknown" as "confirmed unsigned".
        signed = None
        scan_summary = None

        if digest:
            image_key = f"{registry_url}/{repo}@{digest}"
            try:
                referrers = registry_referrers_tool.invoke(
                    {"registry_url": registry_url, "repo": repo, "digest": digest}, config=_cb
                )
                signed, scan_summary = self._classify_referrers(referrers)
                referrers_scope.observed(image_key)
            except Exception as exc:
                # A GENUINE failure (network/5xx/timeout) — NOT the same as
                # the registry cleanly reporting "unsupported" (that's a 404,
                # already turned into a normal, non-raising dict by
                # registry_referrers_tool). `signed`/`scan_summary` stay None
                # (unknown) so a transient outage can never flip a
                # previously-VERIFIED image to "unsigned". The failure is
                # still recorded (via `referrers_scope.failed` below) so the
                # run downgrades to "partial" rather than reporting a clean
                # "completed" that hides the gap.
                logger.info(
                    "ContainerRegistryAgent: referrers lookup failed for %s/%s@%s: %s",
                    registry_url,
                    repo,
                    digest,
                    exc,
                )
                referrers_scope.failed(image_key, exc)

        return {
            "name": f"{registry_url.rstrip('/')}/{repo}:{tag}",
            "type": "container_image",
            "data": {
                "registry": registry_url,
                "repo": repo,
                "tag": tag,
                "digest": digest,
                "signed": signed,
                "scan_result_summary": scan_summary,
            },
        }

    @staticmethod
    def _classify_referrers(referrers: dict | None) -> tuple[bool, dict | None]:
        """Classify an OCI referrers response into (signed, scan_result_summary).

        Returns ``(False, None)`` when the registry doesn't implement the
        Referrers API (``supported: False``) — unknown status, not "clean".
        """
        if not referrers or not referrers.get("supported", False):
            return False, None

        manifests = referrers.get("manifests") or []
        artifact_types = [m.get("artifactType", "") for m in manifests if isinstance(m, dict)]
        signed = any(t.startswith(_SIGNATURE_ARTIFACT_PREFIXES) for t in artifact_types)
        scan_types = [t for t in artifact_types if t.startswith(_SCAN_ARTIFACT_PREFIXES)]

        summary = {
            "referrer_count": len(manifests),
            "artifact_types": sorted(set(artifact_types)),
            "scan_artifact_count": len(scan_types),
        }
        return signed, summary

    def _detail_writers(self, scope, result):
        return [self._write_container_registry_details]

    def _write_container_registry_details(self) -> int:
        """Upsert ``container_images`` rows (and the registry Resource each names).

        P5 (rev11-T5-B): this used to end by emitting PULLED_FROM +
        HAS_VULNERABILITY_SCAN into ``resource_relationships``. Both edges and
        the synthetic ``container_scan`` node are gone — see the module
        docstring's EPITAPH for where each fact lives now. Every detail write is
        untouched, including the ``registry`` Resource upsert, which is kept
        deliberately (it is inventory, not an edge anchor).

        Re-queries the generic Resource rows just written by the base
        collect() phase (matched by domain + name) rather than threading
        resource ids through collect() — mirrors the eol/octopus detail-writer
        pattern.

        L-1: scoped to exactly the images ``collect()`` observed THIS run
        (``self._collected_image_names``, set at the end of ``collect()``) —
        querying every historical ``container_image`` Resource reprocessed
        images this run never even looked at on every single pass. When the
        attribute is absent (e.g. a detail-write phase invoked without a
        preceding ``collect()`` on the same instance — not a path ``run()``
        takes), falls back to the old unscoped query rather than silently
        processing nothing.
        """
        from infra_brain.api._seeding import upsert_resource
        from infra_brain.db.models import Resource

        written = 0
        registry_resource_cache: dict[str, object] = {}
        collected_names = getattr(self, "_collected_image_names", None)

        with get_session() as session:
            query = session.query(Resource).filter_by(domain=self.domain, type="container_image")
            if collected_names is None:
                image_resources = query.all()
            elif not collected_names:
                image_resources = []
            else:
                image_resources = query.filter(Resource.name.in_(collected_names)).all()

            for res in image_resources:
                md = res.metadata_ or {}
                registry_url = md.get("registry", "")
                repo = md.get("repo", "")
                tag = md.get("tag", "")
                digest = md.get("digest", "")
                if not (registry_url and repo and digest):
                    continue

                # L-1: None means "referrers not verified this run" (a
                # transient Referrers-API failure) — distinct from a
                # conclusive check. Only a concrete True/False may overwrite
                # a stored value; on an EXISTING row, None leaves
                # signed/scan_result_summary exactly as they were, so a
                # previously-VERIFIED signed=True survives a re-scan outage
                # instead of silently downgrading to "unsigned".
                signed_value = md.get("signed")
                scan_summary = md.get("scan_result_summary")

                existing = (
                    session.query(ContainerImage)
                    .filter_by(registry=registry_url, repo=repo, digest=digest)
                    .first()
                )
                if existing is not None:
                    existing.tag = tag
                    existing.resource_id = res.id
                    if signed_value is not None:
                        existing.signed = bool(signed_value)
                        existing.scan_result_summary = scan_summary
                    # else: this run couldn't verify referrers for this image
                    # — leave signed/scan_result_summary untouched.
                else:
                    session.add(
                        ContainerImage(
                            resource_id=res.id,
                            registry=registry_url,
                            repo=repo,
                            tag=tag,
                            digest=digest,
                            scan_result_summary=scan_summary,
                            signed=bool(signed_value) if signed_value is not None else False,
                        )
                    )
                written += 1

                # KEPT (the PULLED_FROM edge that used to follow is not): the
                # registry is an inventory entity in its own right. The
                # ``container_scan`` upsert that used to follow the second edge
                # IS gone — see the module docstring's EPITAPH for why the two
                # nodes are treated differently.
                if registry_url not in registry_resource_cache:
                    registry_resource = upsert_resource(
                        session,
                        name=registry_url,
                        domain=self.domain,
                        resource_type="registry",
                        source=type(self).__name__,
                    )
                    registry_resource_cache[registry_url] = registry_resource.id

            session.commit()

        logger.info(
            "ContainerRegistryAgent: upserted %d container_images row(s)",
            written,
        )
        return written
