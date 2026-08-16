"""PKIAgent — PKI / Certificate Authority chain monitoring (GitLab issue #94).

Distinct from per-host TLS expiry (already covered by ``host_posture.py``'s
``HAS_CERTIFICATE`` edge on host-collected leaf certs, written by
``graph_maintenance.py``): this agent tracks the CA infrastructure itself —
root/intermediate CA chain validity, CRL/OCSP responder health, and
cross-references which already-tracked leaf certs (``HostCertificate`` rows)
chain to which tracked intermediate CA.

Data source: this MR does not add a new external CA API integration (none is
configured in ``config.py``, and none is named in the issue) — it DERIVES the
set of in-use CAs from the ``issuer`` strings already collected onto
``host_certificates`` by windows.py/linux.py (the same "derive from already-
collected inventory" shape ``eol.py`` uses for OS products). Each distinct
issuer becomes one tracked ``CertificateAuthority`` row, classified root vs.
intermediate by a self-signed heuristic (issuer == subject for at least one
cert bearing that issuer). ``crl_url``/``ocsp_url`` are populated only when
already present on the tracked row (e.g. a future manual-seed path) — nothing
in ``host_certificates`` carries them today, so a freshly-derived CA has no
CRL/OCSP URL and the health check below is simply skipped for it.

Graph edges:
  * ``ISSUED_BY``   — certificate ISSUED_BY certificate_authority. **DECLARED**
    on ``spec.emits_edges`` and materialised into ``graph_edges`` by
    ``graph_engine``; this module no longer derives it into
    ``resource_relationships``. See the graph-contribution block below.

EPITAPH — the three edges this agent used to hand-write (P5, rev11-T5-B).
``resource_relationships`` is being dropped; every writer had to go, and these
three were the last ones here:

  * ``CHAINS_TO`` (intermediate_ca -> parent CA) — GENUINE, not containment: a
    CA chains to a CA, two independently-referrable entities. It is deleted
    rather than migrated for the reason TRK-358 already recorded — the live
    table holds ZERO CHAINS_TO rows, so there is nothing to prove a declaration
    equivalent to, and the two-commit equivalence discipline refuses a
    declaration whose oracle is an empty set. THE PATH IF CHAINS EVER
    MATERIALISE: declare it exactly like ``ISSUED_BY`` above — an ``EdgeSpec``
    over ``_CA_NODE`` on both ends, ``from_key`` = the CA's ``issuer``
    attribute, ``to_key`` = ``natural_key`` (the parent's name), FORWARD, since
    a CA carries exactly one issuer DN and the join is functional from the
    child side. ``_CA_NODE`` already carries the rows; only ``attributes``
    needs ``issuer`` added and a new ``EdgeSpec`` appended to ``emits_edges``.
    WHERE THE FACT LIVES MEANWHILE: ``certificate_authorities.issuer``, the
    column ``_derive_cas`` writes and the deleted edge merely restated.
  * ``HAS_CRL`` / ``HAS_OCSP_RESPONDER`` — containment under §3.1 (a responder
    URL is a property of the CA, not a peer entity), so they were never
    migration targets. WHERE THE FACT LIVES NOW: unchanged —
    ``certificate_authorities.crl_url`` / ``.crl_status`` / ``.crl_checked_at``
    and the ocsp_* triple. The PROBE that writes those columns is untouched;
    only the edge to a synthetic ``pki/crl_responder`` node went with them, and
    that node is gone too — nothing in ``src/`` ever read it, and with the edge
    deleted it could not be reached from the CA it described.

Read-only: the only external calls are GET-only health probes (via
``tools.http_readonly.readonly_get``) against a CA's own ``crl_url``/
``ocsp_url`` — a simplified reachability check (HTTP GET, 2xx/3xx =
"reachable"), not a full CRL-parse or OCSP protocol exchange. Everything else
is a read of already-collected Postgres rows.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from infra_brain.api._seeding import upsert_resource
from infra_brain.db.models import ZONE_CORPORATE, CertificateAuthority, HostCertificate
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, EdgeSpec, NodeSpec, Tier
from infra_brain.tools.pki import crl_ocsp_probe_tool

logger = logging.getLogger(__name__)

# --- graph contribution -----------------------------------------------------
#
# ``ISSUED_BY`` was one of four relationship types P3 counted and refused to
# backfill because nothing DECLARED them — an undeclared type has no maintainer,
# so copying it into ``graph_edges`` would mint ``authority='auto'`` rows nothing
# would ever refresh or retire. Judged against the design doc's §3.1 test — *does
# it connect two things that can each exist, and be referred to, independently?*
# — this one is unambiguously GENUINE, and it is the easiest of the four to
# declare, because **both ends are already pki's own ``resources`` rows**:
#
#   * ``security/certificate``        — written by ``_upsert_certificate_resources``
#     below, one per observed leaf-cert thumbprint.
#   * ``pki/certificate_authority``   — written by ``_upsert_ca_rows``, one per
#     distinct issuer DN.
#
# A CA is not an attribute of a certificate: it outlives any one cert, issues
# many of them, chains to a parent of its own (``CHAINS_TO``), and has its own
# CRL/OCSP responders. "Which certs does this CA issue" is exactly the one-hop
# question the graph exists to answer, and it is unanswerable from a string
# sitting in ``host_certificates.issuer``.
#
# NO ``resource_backed=False`` NEEDED, unlike ``ContainerImage``: pki already
# materialises the CA as an inventory item, so the node has a real owning row.

_CERTIFICATE_NODE = NodeSpec(
    type="Certificate",
    resource_type="certificate",
    # The cert rows live in the ``security`` domain, not pki's own — pki writes
    # them there deliberately, alongside graph_maintenance's HAS_CERTIFICATE
    # convergence nodes, so both stores agree on one row per thumbprint.
    domain="security",
    # ``resources.name`` IS the thumbprint (see _upsert_certificate_resources).
    natural_key="name",
    # ``issuer`` is the join key and MUST ride onto the node: the engine matches
    # graph-side, so a value left in resources.metadata is invisible to the edge.
    attributes=("thumbprint", "subject", "issuer"),
)

_CA_NODE = NodeSpec(
    type="CertificateAuthority",
    resource_type="certificate_authority",
    natural_key="name",
    attributes=("ca_type",),
)

# ``<Certificate> ─ISSUED_BY→ <CertificateAuthority>``.
#
# Functional by construction: a certificate carries exactly ONE issuer DN, and
# each DN is one CA row, so the join needs neither ``from_key_multi`` nor
# ``INVERSE``. A DN two CA nodes claimed would be refused by ``_target_index``
# rather than guessed — which is the honest answer, since the live store does
# hold two near-duplicate ACCVRAIZ1 rows differing only in DN whitespace (a
# historical normalisation change). They are distinct KEYS, so they are distinct
# nodes and the live cert resolves to exactly the one its own ``issuer`` string
# names; the stale row simply attracts no edge, which is the correct outcome and
# not something the engine should paper over.
#
# ``key_normalizer="exact"`` — a DN is a structured identifier, not a hostname.
# Folding case or separators here would merge CAs that are genuinely different
# (``CN=x,O=A`` and ``CN=x,O=B`` share no meaning), and the whitespace variance
# above is precisely a case where silently merging would ASSERT an identity
# nobody established. Cross-source identity between near-duplicate CAs is the
# reconciler's job, not a normaliser's.
#
# ``deterministic_match`` / 0.900 — a DN STRING matched against a separately
# derived CA entity, exactly what the deleted deriver claimed (0.9) and exactly
# what the confidence-honesty rule forbids calling 1.000.
_ISSUED_BY_EDGE = EdgeSpec(
    type="ISSUED_BY",
    from_node=_CERTIFICATE_NODE.type,
    to_node=_CA_NODE.type,
    from_key="attributes.issuer",
    to_key="name",
    key_normalizer="exact",
    method="deterministic_match",
    confidence=Decimal("0.900"),
)


class PKIAgent(ETLConnector):
    # REASONER, not COLLECTOR: like eol.py, this agent does not enumerate an
    # external fleet directly — it derives its resource set (tracked CAs)
    # from host_certificates rows already written by the windows/linux
    # collectors, so it belongs after them in the sweep's reasoner tier. Its
    # only external calls (CRL/OCSP GETs) are read-only health probes against
    # already-known URLs, not fleet enumeration.
    spec = AgentSpec(
        domain="pki",
        tier=Tier.REASONER,
        schedule="40 2 * * *",
        max_staleness=None,
        skip_hook=True,
        # ISSUED_BY is the only one of this agent's four edge types that is a
        # relationship between two independently-referrable entities. CHAINS_TO
        # is the same shape and could follow (CA → parent CA, both pki's own
        # rows); it is deliberately NOT declared in this pass because it has
        # zero live rows, so there is nothing to prove a declaration against and
        # nothing for the P4 backfill to carry. HAS_CRL / HAS_OCSP_RESPONDER are
        # containment by §3.1 — a responder URL is a property of the CA, and the
        # "node" they point at is that URL restated as a resource.
        emits_nodes=(_CERTIFICATE_NODE, _CA_NODE),
        emits_edges=(_ISSUED_BY_EDGE,),
        # A CA is named by its distinguished name; a cert by its thumbprint.
        identity_keys=("name", "thumbprint"),
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        """Derive tracked CAs from host_certificates issuer strings.

        Stashes the derived CA info on ``self._derived_cas`` and the source
        cert rows on ``self._cert_rows`` for the detail-write phase (registry
        upsert + edge emission), mirroring eol.py's collect()/_detail_writers
        split.
        """
        try:
            with get_session() as session:
                cert_rows = (
                    session.query(
                        HostCertificate.thumbprint,
                        HostCertificate.subject,
                        HostCertificate.issuer,
                        HostCertificate.not_before,
                        HostCertificate.not_after,
                    )
                    .filter(HostCertificate.issuer.isnot(None))
                    .filter(HostCertificate.issuer != "")
                    .all()
                )
        except Exception as exc:
            logger.warning("PKIAgent: failed to scan host_certificates: %s", exc)
            raise

        self._cert_rows = cert_rows
        derived = self._derive_cas(cert_rows)
        self._derived_cas = derived

        if not derived:
            logger.info("PKIAgent: no CA issuer strings found in host_certificates")
            return []

        items = []
        for info in derived.values():
            items.append(
                {
                    "name": info["name"],
                    "type": "certificate_authority",
                    "data": {
                        "ca_type": info["ca_type"],
                        "issuer": info["issuer"],
                        "not_before": info["not_before"].isoformat()
                        if info["not_before"]
                        else None,
                        "not_after": info["not_after"].isoformat() if info["not_after"] else None,
                    },
                }
            )
        return items

    def _detail_writers(self, scope, result):
        return [self._write_pki_registry]

    # --- CA derivation -------------------------------------------------

    @staticmethod
    def _derive_cas(cert_rows) -> dict:
        """Distinct issuer strings -> {name, ca_type, issuer, not_before, not_after}.

        A CA's own PARENT (who issued *it*) can only be learned from a cert
        row whose ``subject`` IS the CA's name — i.e. the CA's own
        certificate, present in some host's trust store (e.g. an
        intermediate or root CA cert deployed to ``LocalMachine/Root`` or
        ``LocalMachine/CA``). A leaf cert's ``issuer`` field only tells us
        the CA *exists*, never who issued that CA in turn.

        ``ca_type`` is "root" when the CA's own cert row is self-signed
        (``subject == issuer``); "intermediate" otherwise, including the
        case where no self-cert row was observed at all (conservative
        default — a CA referenced only as a leaf-cert issuer, with no chain
        evidence either way, is assumed intermediate rather than silently
        promoted to root). ``issuer`` on the returned entry is the CA's OWN
        parent (``None`` if unknown), used by ``_build_chains_to_edges``.

        ``not_before``/``not_after`` take the widest observed window across
        every row referencing this CA (as issuer, or as its own subject) —
        a best-effort chain-validity proxy in the absence of a dedicated CA
        cert table.
        """
        issuers: set[str] = set()
        for row in cert_rows:
            issuer = (row.issuer or "").strip()
            if issuer:
                issuers.add(issuer)

        # CA name -> issuer recorded on the row whose subject IS that CA
        # (i.e. the CA's own certificate). Prefers a self-signed observation.
        self_cert_issuer: dict[str, str] = {}
        for row in cert_rows:
            subject = (row.subject or "").strip()
            issuer = (row.issuer or "").strip()
            if subject in issuers and (subject not in self_cert_issuer or subject == issuer):
                self_cert_issuer[subject] = issuer

        derived: dict = {}
        for name in issuers:
            parent = self_cert_issuer.get(name)
            ca_type = "root" if parent == name else "intermediate"
            derived[name] = {
                "name": name,
                "issuer": parent,
                "ca_type": ca_type,
                "not_before": None,
                "not_after": None,
            }

        for row in cert_rows:
            issuer = (row.issuer or "").strip()
            subject = (row.subject or "").strip()
            for name in (issuer, subject):
                entry = derived.get(name)
                if entry is None:
                    continue
                if row.not_before and (
                    entry["not_before"] is None or row.not_before < entry["not_before"]
                ):
                    entry["not_before"] = row.not_before
                if row.not_after and (
                    entry["not_after"] is None or row.not_after > entry["not_after"]
                ):
                    entry["not_after"] = row.not_after
        return derived

    # --- registry write ---------------------------------------------------

    def _write_pki_registry(self) -> None:
        """Upsert the CA registry + the declared-node certificate rows, and
        refresh CRL/OCSP responder health.

        P5 (rev11-T5-B): this used to end with an ``emit_edges_batch`` writing
        CHAINS_TO + HAS_CRL + HAS_OCSP_RESPONDER into ``resource_relationships``.
        That store is being dropped; the epitaph for all three (including the
        declaration path CHAINS_TO would take if chains ever materialise) is in
        the module docstring. Every WRITE that was interleaved with the edge
        build survives verbatim: the ``certificate_authorities`` rows, the
        ``security/certificate`` Resource rows the declared ``Certificate`` node
        reads, and the crl_status/crl_checked_at/ocsp_status/ocsp_checked_at
        columns the responder probe stamps.
        """
        derived = getattr(self, "_derived_cas", None)
        if not derived:
            return

        now = datetime.now(UTC)
        with get_session() as session:
            ca_resource_ids = self._upsert_ca_rows(session, derived, now)
            session.commit()

            # The certificate rows the declared ``Certificate`` node reads. No
            # longer paired with an edge write — see the method's docstring.
            certs = self._upsert_certificate_resources(session, ca_resource_ids)

            probed = self._probe_responders(session, derived, now)
            session.commit()
        logger.info(
            "PKIAgent: pki registry upserted %d CA(s) and %d certificate(s), probed %d responder(s)",
            len(derived),
            certs,
            probed,
        )

    def _upsert_ca_rows(self, session, derived: dict, now) -> dict:
        """Upsert Resource + CertificateAuthority per derived CA. Returns {name: resource_id}."""
        ca_resource_ids: dict[str, object] = {}
        for name, info in derived.items():
            res = upsert_resource(
                session,
                name=name,
                domain="pki",
                resource_type="certificate_authority",
                zone=ZONE_CORPORATE,
                source=type(self).__name__,
                metadata={"ca_type": info["ca_type"]},
            )
            session.flush()
            ca_resource_ids[name] = res.id

            existing = (
                session.query(CertificateAuthority)
                .filter_by(name=name, ca_type=info["ca_type"])
                .first()
            )
            if existing is not None:
                existing.issuer = info["issuer"]
                existing.not_before = info["not_before"]
                existing.not_after = info["not_after"]
                existing.collected_at = now
                existing.resource_id = res.id
            else:
                session.add(
                    CertificateAuthority(
                        resource_id=res.id,
                        name=name,
                        ca_type=info["ca_type"],
                        issuer=info["issuer"],
                        not_before=info["not_before"],
                        not_after=info["not_after"],
                        collected_at=now,
                    )
                )
        return ca_resource_ids

    # ``_build_chains_to_edges`` was DELETED here (P5, rev11-T5-B) — it built
    # intermediate_ca CHAINS_TO parent_ca edges for the legacy store and did
    # nothing else. See the module docstring's EPITAPH for the verdict (genuine,
    # zero live rows, declaration deferred for want of an oracle) and for the
    # exact EdgeSpec that reinstates it if the population ever becomes non-empty.

    def _upsert_certificate_resources(self, session, ca_resource_ids: dict) -> int:
        """Materialise one ``security/certificate`` row per observed leaf cert.

        Reuses the SAME security/certificate convergence node
        graph_maintenance's HAS_CERTIFICATE emitter uses (get-or-create on the
        ``(domain, type, name=thumbprint)`` natural key) rather than depending
        on graph_maintenance having already run.

        THIS USED TO ALSO EMIT ``ISSUED_BY`` into ``resource_relationships``.
        That half is gone — the relationship is declared on ``spec.emits_edges``
        and materialised into ``graph_edges``, which unlike
        ``UNIQUE(from, to, type)`` can record that a certificate was reissued by
        a different CA instead of overwriting its own history. The equivalence
        was machine-checked against a verbatim copy of the deleted block before
        it was deleted, and that copy keeps running in
        ``tests/agents/test_pki_issued_by_graph.py``.

        The RESOURCE write deliberately stays, and this method exists only for
        it. The declared ``Certificate`` node reads these rows; deleting them
        alongside the edge would have left the declaration pointing at a
        population nothing creates — the failure mode iac's adoption of
        ``ContainerImage`` exists to avoid.

        ``ca_resource_ids`` still gates the write for the same reason it always
        did: a cert whose issuer we could not derive a CA row for is not a
        tracked-PKI fact, and materialising it would inflate the certificate
        population with rows no edge will ever reach.
        """
        written = 0
        cert_rows = getattr(self, "_cert_rows", None) or []
        for row in cert_rows:
            thumb = (row.thumbprint or "").strip() if hasattr(row, "thumbprint") else ""
            issuer = (row.issuer or "").strip()
            if not thumb or not issuer:
                continue
            if ca_resource_ids.get(issuer) is None:
                continue
            upsert_resource(
                session,
                name=thumb,
                domain="security",
                resource_type="certificate",
                zone=ZONE_CORPORATE,
                source=type(self).__name__,
                metadata={"thumbprint": thumb, "subject": row.subject or "", "issuer": issuer},
            )
            session.flush()
            written += 1
        return written

    def _probe_responders(self, session, derived: dict, now) -> int:
        """GET-only health check against crl_url/ocsp_url when already set on
        the tracked CertificateAuthority row. Returns the number of probes made.

        P5 (rev11-T5-B): formerly ``_check_responders_and_build_edges``. The
        probe and the four columns it stamps (``crl_status``/``crl_checked_at``
        and the ocsp_* pair) are UNCHANGED — they are the reason this method
        exists. What left with the legacy store is the HAS_CRL /
        HAS_OCSP_RESPONDER edge each probe used to pair itself with, and the
        synthetic ``pki/crl_responder`` / ``pki/ocsp_responder`` Resource each
        edge pointed at. Those nodes are deleted rather than orphaned: a grep of
        ``src/`` finds no reader outside this file's own emission, the URL they
        were keyed by is already the CA's own ``crl_url``/``ocsp_url`` column,
        and with the edge gone nothing could navigate from the responder back to
        the CA it belonged to. ``ca_resource_ids`` is no longer a parameter —
        it existed only to supply the edge's ``from_id``.
        """
        probed = 0
        for name in derived:
            ca_row = session.query(CertificateAuthority).filter_by(name=name).first()
            if ca_row is None:
                continue

            if ca_row.crl_url:
                ca_row.crl_status = self._probe_url(ca_row.crl_url)
                ca_row.crl_checked_at = now
                probed += 1

            if ca_row.ocsp_url:
                ca_row.ocsp_status = self._probe_url(ca_row.ocsp_url)
                ca_row.ocsp_checked_at = now
                probed += 1
        return probed

    def _probe_url(self, url: str) -> str:
        """Read-only reachability probe via ``crl_ocsp_probe_tool``.

        Routed through the tool + ``self.callbacks`` (rather than calling
        ``readonly_get`` directly) so every probe produces an audit-log row
        and is subject to read-only enforcement and DLP scanning like every
        other external call in this codebase — caught by lc-safety-reviewer
        during Phase E integration review.
        """
        return crl_ocsp_probe_tool.invoke({"url": url}, config={"callbacks": self.callbacks})
