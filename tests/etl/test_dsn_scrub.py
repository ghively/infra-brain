"""SEC-2 — DSN/connection-string credentials must never reach a persisted or
returned error message.

``ETLConnector.run()`` is the shared error-capture path every collector funnels
through: any exception from ``collect()`` becomes ``CollectionRun.error_message``
(persisted, readable via the dashboard/API) and ``CollectionResult.errors``. If
that exception originated from a DB-layer failure (e.g. a malformed DSN raising
``sqlalchemy.exc.ArgumentError`` with the full connection string — credentials
included — embedded verbatim), the raw credentials would otherwise be exposed.
"""

from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectionResult
from infra_brain.db.models import CollectionRun
from infra_brain.etl.base import scrub_dsn

_LEAKY_DSN = "postgresql://svc_user:s3cr3t-pw@db.internal:5432/infra"


def test_scrub_dsn_redacts_credentials_keeps_host_visible():
    message = f"could not connect: {_LEAKY_DSN}"
    scrubbed = scrub_dsn(message)
    assert "s3cr3t-pw" not in scrubbed
    assert "svc_user" not in scrubbed
    assert "[REDACTED]@" in scrubbed
    # Host/port/db stay visible — useful for triage, not sensitive on their own.
    assert "db.internal:5432/infra" in scrubbed


def test_scrub_dsn_leaves_plain_https_url_untouched():
    """Most collectors talk plain HTTPS APIs with no embedded credentials — a
    plain host error is genuinely useful for triage and must not be redacted."""
    message = "Octopus server info failed: GET https://octopus.example.com/api/3.3.8 timed out"
    assert scrub_dsn(message) == message


def test_scrub_dsn_handles_none_and_empty():
    assert scrub_dsn(None) is None
    assert scrub_dsn("") == ""


class _LeakyDsnAgent(BaseAgent):
    domain = "test"

    def collect(self, scope: str = "all"):
        raise RuntimeError(f"connection failed: {_LEAKY_DSN}")


def test_run_scrubs_dsn_from_error_message_and_result_errors(engine):
    """A collect() failure whose message embeds a DSN must have its credentials
    scrubbed in both CollectionRun.error_message (persisted) and
    CollectionResult.errors (returned)."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        with patch("infra_brain.agents.base.get_chat_model"):
            agent = _LeakyDsnAgent()
            result = agent.run(trigger_type="scheduled", scope="all")

    assert isinstance(result, CollectionResult)
    assert result.status == "failed"
    assert len(result.errors) == 1
    assert "s3cr3t-pw" not in result.errors[0]
    assert "svc_user" not in result.errors[0]

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run.status == "failed"
        assert "s3cr3t-pw" not in run.error_message
        assert "svc_user" not in run.error_message
