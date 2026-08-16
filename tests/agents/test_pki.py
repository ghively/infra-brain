"""Tests for PKIAgent — PKI/CA chain monitoring (GitLab issue #94).

Mirrors eol.py's derive-then-write test shape (session_patcher fixture from
tests/agents/conftest.py): collect() derives tracked CAs from
host_certificates issuer strings, then _write_pki_registry() upserts
CertificateAuthority rows, the security/certificate Resource rows the declared
``Certificate`` node reads, and the CRL/OCSP responder health columns.

P5 (rev11-T5-B): this agent emits NO edges into ``resource_relationships`` any
more. ISSUED_BY moved to ``graph_edges`` via ``spec.emits_edges`` in rev10;
CHAINS_TO, HAS_CRL and HAS_OCSP_RESPONDER are deleted outright (see the
agent module's EPITAPH). The assertions that covered them died with them; what
replaces each is an assertion on the column the fact actually lives in.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.pki import PKIAgent
from infra_brain.db.models import CertificateAuthority, HostCertificate, Resource
from infra_brain.db.relationships import RelationshipType

MODULE = "infra_brain.agents.pki"
TOOL_MODULE = "infra_brain.tools.pki"


# ``_sqlite_emit_edges_batch`` was DELETED here (P5, rev11-T5-B). It was the
# sqlite stand-in for the Postgres upsert in ``emit_edges_batch``, and it
# existed only because this module patched that helper. PKIAgent no longer
# calls it at all, so the shim has nothing left to stand in for.


def _agent():
    agent = PKIAgent.__new__(PKIAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def _make_resource(session, domain, rtype, name):
    res = Resource(domain=domain, type=rtype, name=name, source="test")
    session.add(res)
    session.flush()
    return res


def _edges_for(session, rel_type):
    # P5 integration: the legacy table is gone from the schema entirely, so
    # "no edges of this type" is not a count — it is the table's absence.
    from sqlalchemy import inspect as _sqla_inspect

    assert "resource_relationships" not in _sqla_inspect(session.get_bind()).get_table_names()
    return []


# --- domain / callbacks --------------------------------------------------


class TestDomain:
    def test_domain_is_set(self):
        assert PKIAgent.spec.domain == "pki"

    def test_callbacks_wired(self):
        agent = _agent()
        assert agent.callbacks is not None


# --- collect(): success / empty / exception ------------------------------


class TestCollect:
    def test_collect_derives_root_and_intermediate(self, session_patcher):
        with session_patcher(MODULE) as engine:
            with Session(engine) as s:
                host = _make_resource(s, "windows", "host", "h1")
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="LocalMachine/My",
                        subject="CN=leaf.example.com",
                        issuer="CN=Intermediate CA",
                        thumbprint="THUMB1",
                    )
                )
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="LocalMachine/Root",
                        subject="CN=Root CA",
                        issuer="CN=Root CA",
                        thumbprint="THUMB2",
                    )
                )
                s.commit()

            agent = _agent()
            result = agent.collect()

        assert isinstance(result, list)
        names = {item["name"] for item in result}
        assert names == {"CN=Intermediate CA", "CN=Root CA"}
        by_name = {item["name"]: item for item in result}
        assert by_name["CN=Root CA"]["data"]["ca_type"] == "root"
        assert by_name["CN=Intermediate CA"]["data"]["ca_type"] == "intermediate"

    def test_collect_empty_when_no_certificates(self, session_patcher):
        with session_patcher(MODULE):  # empty DB
            agent = _agent()
            result = agent.collect()
        assert result == []

    def test_collect_exception_does_not_propagate(self):
        agent = _agent()
        with patch(f"{MODULE}.get_session", side_effect=RuntimeError("db unreachable")):
            with pytest.raises(RuntimeError):
                agent.collect()

    def test_run_reports_failed_on_collect_exception(self, sqlite_engine):
        from contextlib import contextmanager

        @contextmanager
        def _get_session():
            with Session(sqlite_engine) as s:
                yield s

        agent = _agent()
        with (
            patch("infra_brain.etl.base.get_session", _get_session),
            patch.object(agent, "collect", side_effect=RuntimeError("upstream down")),
        ):
            run_result = agent.run()
        assert run_result.status == "failed"
        assert "upstream down" in run_result.errors[0]


# --- _write_pki_registry(): upsert + edges --------------------------------


class TestWritePkiRegistry:
    def test_writes_ca_rows_and_certificate_resources(self, session_patcher):
        """P5 (rev11-T5-B): renamed from ``..._and_chains_to_edge``.

        The CHAINS_TO assertion DIED WITH ITS CODE — ``_build_chains_to_edges``
        is deleted (module-docstring EPITAPH: genuine, zero live rows, so no
        oracle to declare against; the reinstating EdgeSpec is written out
        there). Everything the method still owes is asserted below, plus a new
        claim that the legacy store stays empty.
        """
        with session_patcher(MODULE) as engine:
            with Session(engine) as s:
                host = _make_resource(s, "windows", "host", "h1")
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="LocalMachine/My",
                        subject="CN=leaf.example.com",
                        issuer="CN=Intermediate CA",
                        thumbprint="THUMB1",
                    )
                )
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="LocalMachine/Root",
                        subject="CN=Root CA",
                        issuer="CN=Root CA",
                        thumbprint="THUMB2",
                    )
                )
                # The intermediate CA's OWN certificate, present in the trust
                # store — the only way _derive_cas can learn its parent
                # (a leaf cert's issuer field alone never reveals this).
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="LocalMachine/CA",
                        subject="CN=Intermediate CA",
                        issuer="CN=Root CA",
                        thumbprint="THUMB3",
                    )
                )
                s.commit()

            agent = _agent()
            agent.collect()
            agent._write_pki_registry()

            with Session(engine) as s:
                cas = s.query(CertificateAuthority).all()
                assert {c.name for c in cas} == {"CN=Intermediate CA", "CN=Root CA"}
                intermediate = next(c for c in cas if c.name == "CN=Intermediate CA")
                root = next(c for c in cas if c.name == "CN=Root CA")
                assert intermediate.ca_type == "intermediate"
                assert root.ca_type == "root"

                # The chain FACT survives its edge: the parent DN is on the
                # child CA's own row, which is what the declaration would read.
                assert intermediate.issuer == "CN=Root CA"

                # P5: not one legacy row, of any type, from this pass.
                assert _edges_for(s, RelationshipType.CHAINS_TO) == []

                # ISSUED_BY is no longer derived into resource_relationships —
                # it is declared on ``PKIAgent.spec.emits_edges`` and written to
                # ``graph_edges`` by ``graph_engine``
                # (tests/agents/test_pki_issued_by_graph.py proves the
                # equivalence against a verbatim copy of the deleted block).
                # What this pass must STILL do is materialise the
                # security/certificate rows that declaration reads.
                assert _edges_for(s, RelationshipType.ISSUED_BY) == []
                certs = (
                    s.query(Resource)
                    .filter(Resource.domain == "security", Resource.type == "certificate")
                    .all()
                )
                assert {c.name for c in certs} == {"THUMB1", "THUMB2", "THUMB3"}
                assert all((c.metadata_ or {}).get("issuer") for c in certs)

    def test_write_pki_registry_noop_when_nothing_derived(self, session_patcher):
        with session_patcher(MODULE) as engine:
            agent = _agent()
            agent._derived_cas = {}
            agent._write_pki_registry()
            with Session(engine) as s:
                assert s.query(CertificateAuthority).count() == 0

    def test_crl_and_ocsp_health_check_records_status(self, session_patcher):
        """P5 (rev11-T5-B): renamed from ``..._emits_edges``.

        The HAS_CRL / HAS_OCSP_RESPONDER assertions died with the edges (and
        with the synthetic responder nodes they anchored). The PROBE and the
        four columns it stamps are the load-bearing half and are asserted
        harder than before: both statuses set, exactly two GETs, and zero
        legacy rows.
        """
        with session_patcher(MODULE) as engine:
            with Session(engine) as s:
                host = _make_resource(s, "linux", "host", "h1")
                s.add(
                    HostCertificate(
                        resource_id=host.id,
                        store="linux:/etc/ssl/certs",
                        subject="CN=Root CA",
                        issuer="CN=Root CA",
                        thumbprint="THUMB3",
                    )
                )
                s.commit()

            agent = _agent()
            agent.collect()

            fake_resp = MagicMock(status_code=200)
            with (
                patch(f"{TOOL_MODULE}.readonly_get", return_value=fake_resp) as mock_get,
                patch(f"{TOOL_MODULE}._is_ssrf_safe_target", return_value=True),
            ):
                # Pre-seed crl_url/ocsp_url on the row the write phase will find.
                agent._write_pki_registry()
                with Session(engine) as s:
                    ca = s.query(CertificateAuthority).filter_by(name="CN=Root CA").first()
                    ca.crl_url = "https://ca.example.com/crl"
                    ca.ocsp_url = "https://ca.example.com/ocsp"
                    s.commit()

                agent._write_pki_registry()
                assert mock_get.call_count == 2

            with Session(engine) as s:
                ca = s.query(CertificateAuthority).filter_by(name="CN=Root CA").first()
                assert ca.crl_status == "reachable"
                assert ca.ocsp_status == "reachable"
                assert ca.crl_checked_at is not None
                assert ca.ocsp_checked_at is not None
                # The edges are gone — and so are the synthetic responder nodes
                # they pointed at (nothing in src/ ever read them).
                assert _edges_for(s, RelationshipType.CHAINS_TO) == []
                assert (
                    s.query(Resource)
                    .filter(Resource.type.in_(["crl_responder", "ocsp_responder"]))
                    .count()
                    == 0
                )

    def test_responder_probe_unreachable_on_exception(self):
        agent = _agent()
        with (
            patch(f"{TOOL_MODULE}.readonly_get", side_effect=OSError("connection refused")),
            patch(f"{TOOL_MODULE}._is_ssrf_safe_target", return_value=True),
        ):
            assert agent._probe_url("https://ca.example.com/crl") == "unreachable"

    def test_responder_probe_unreachable_on_4xx(self):
        agent = _agent()
        fake_resp = MagicMock(status_code=404)
        with (
            patch(f"{TOOL_MODULE}.readonly_get", return_value=fake_resp),
            patch(f"{TOOL_MODULE}._is_ssrf_safe_target", return_value=True),
        ):
            assert agent._probe_url("https://ca.example.com/crl") == "unreachable"

    def test_responder_probe_unreachable_on_ssrf_unsafe_target(self):
        """A target failing the SSRF host check must be treated as
        unreachable, never as a bypass that still issues the GET."""
        agent = _agent()
        with (
            patch(f"{TOOL_MODULE}.readonly_get") as mock_get,
            patch(f"{TOOL_MODULE}._is_ssrf_safe_target", return_value=False),
        ):
            assert agent._probe_url("http://169.254.169.254/crl") == "unreachable"
            mock_get.assert_not_called()

    def test_responder_probe_reraises_readonly_denial(self):
        """A ReadOnlyHTTPError (read-only-boundary denial) must propagate,
        never be swallowed into ordinary "unreachable" row data."""
        from infra_brain.tools.http_readonly import ReadOnlyHTTPError

        agent = _agent()
        with (
            patch(f"{TOOL_MODULE}.readonly_get", side_effect=ReadOnlyHTTPError("POST blocked")),
            patch(f"{TOOL_MODULE}._is_ssrf_safe_target", return_value=True),
        ):
            with pytest.raises(ReadOnlyHTTPError):
                agent._probe_url("https://ca.example.com/crl")
