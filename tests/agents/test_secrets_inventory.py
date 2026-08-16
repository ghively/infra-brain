"""Tests for SecretsInventoryAgent — metadata-only secrets inventory (issue #98).

Beyond the standard success / empty / exception trio, this module carries the
REGRESSION tests that encode the safety bar: no value-shaped field may exist on
``SecretRecord`` or on the collector's structured output, and no code path may
construct a value-returning URL.

Note the fixtures below deliberately contain NO ``value``/``data`` payload
field. That is itself part of the proof: if the collector ever needed a value
field to be present in order to work, a fixture without one would fail — the
green test IS the evidence that no code path handles a value.
"""

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from infra_brain.agents.secrets_inventory import (
    SecretsInventoryAgent,
    SecretValueEndpointError,
    _assert_metadata_only,
    bitwarden_list_secret_metadata_tool,
    vault_list_secret_names_tool,
)
from infra_brain.db.models import SECRET_RECORD_COLUMNS, SecretRecord
from infra_brain.etl.base import CollectorSkipped

MODULE = "infra_brain.agents.secrets_inventory"

# Anything that smells like secret material. Applied to ORM column names, to
# the collector's structured-output keys, and to the module source.
_VALUE_SHAPED = re.compile(
    r"value|secret_data|plaintext|cipher|ciphertext|sealed|payload|"
    r"password|passphrase|token|credential|hash|digest|fingerprint",
    re.IGNORECASE,
)

# --- fixtures: metadata-only response bodies, by construction -----------------

VAULT_LIST_BODY = {"data": {"keys": ["db-dsn", "smtp-relay", "nested/"]}}
VAULT_META_BODY = {
    "data": {
        "created_time": "2026-01-02T03:04:05Z",
        "updated_time": "2026-06-01T12:00:00Z",
        "current_version": 3,
        "custom_metadata": {"referencing_system": "payments-api"},
    }
}
BITWARDEN_LIST_BODY = {
    "object": "list",
    "data": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "organizationId": "org-1",
            "key": "PAYMENTS_DB",
            "creationDate": "2026-01-02T03:04:05Z",
            "revisionDate": "2026-06-01T12:00:00Z",
            "projects": [{"id": "p1", "name": "payments"}],
        }
    ],
}


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class _FakeClient:
    """Stand-in for ReadOnlyClient that records every URL actually requested."""

    requested: list[str] = []

    def __init__(self, router, status_code=200):
        self._router = router
        self._status = status_code

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        type(self).requested.append(url)
        if self._status >= 400:
            return _FakeResponse({"errors": ["nope"]}, self._status)
        return _FakeResponse(self._router(url, params), self._status)


def _settings(**overrides):
    base = dict(
        secrets_inventory_backend="vault",
        secrets_inventory_url="https://vault.example",
        secrets_inventory_token="t0ken",
        secrets_inventory_paths="kv/apps",
        secrets_inventory_ssl_verify=True,
        secrets_inventory_max_secrets=2000,
        api_timeout_seconds=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _vault_router(url, params):
    if params and params.get("list") == "true":
        return VAULT_LIST_BODY
    return VAULT_META_BODY


def _agent(settings):
    agent = SecretsInventoryAgent.__new__(SecretsInventoryAgent)
    agent.settings = settings
    agent.callbacks = []
    return agent


@pytest.fixture(autouse=True)
def _reset_requested():
    _FakeClient.requested = []
    yield
    _FakeClient.requested = []


# --- success -----------------------------------------------------------------


def test_collect_vault_success_metadata_only():
    settings = _settings()
    client = _FakeClient(_vault_router)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    assert outcome.errors == []
    names = sorted(i["name"] for i in outcome.items)
    # "nested/" is a folder marker, not a secret — skipped, not recursed into.
    assert names == ["vault:kv/apps/db-dsn", "vault:kv/apps/smtp-relay"]
    data = outcome.items[0]["data"]
    assert data["backend"] == "vault"
    assert data["secret_path"] == "kv/apps/db-dsn"
    assert data["last_rotated_at"] == "2026-06-01T12:00:00+00:00"
    assert data["referencing_system"] == "payments-api"
    # Every URL actually requested went to the metadata mount.
    assert _FakeClient.requested
    assert all("/metadata" in u for u in _FakeClient.requested)
    assert not any("/v1/kv/data/" in u for u in _FakeClient.requested)


def test_collect_bitwarden_success_metadata_only():
    settings = _settings(
        secrets_inventory_backend="bitwarden",
        secrets_inventory_url="https://bw.example",
        secrets_inventory_paths="22222222-2222-2222-2222-222222222222",
    )
    client = _FakeClient(lambda url, params: BITWARDEN_LIST_BODY)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    assert outcome.errors == []
    assert len(outcome.items) == 1
    data = outcome.items[0]["data"]
    assert data["secret_path"].endswith("/PAYMENTS_DB")
    assert data["referencing_system"] == "payments"
    # The per-secret value endpoint /api/secrets/<id> was never touched.
    assert all(u.endswith("/secrets") and "/organizations/" in u for u in _FakeClient.requested)


# --- empty / unconfigured ----------------------------------------------------


def test_collect_unconfigured_is_skipped_not_empty_completed():
    settings = _settings(secrets_inventory_backend="")
    with pytest.raises(CollectorSkipped):
        _agent(settings).collect()


def test_collect_configured_without_credentials_is_skipped():
    settings = _settings(secrets_inventory_token="")
    with pytest.raises(CollectorSkipped):
        _agent(settings).collect()


def test_collect_configured_without_paths_is_skipped():
    settings = _settings(secrets_inventory_paths="  , ")
    with pytest.raises(CollectorSkipped):
        _agent(settings).collect()


def test_aws_backend_is_skipped_deliberately():
    """AWS SM needs a POST-capable client, which this module must not hold."""
    settings = _settings(secrets_inventory_backend="aws")
    with pytest.raises(CollectorSkipped) as exc:
        _agent(settings).collect()
    assert "not supported" in str(exc.value)


def test_collect_empty_backend_listing_yields_no_items():
    settings = _settings()
    client = _FakeClient(lambda url, params: {"data": {"keys": []}})
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()
    assert outcome.items == []
    assert outcome.errors == []


# --- exception handling ------------------------------------------------------


def test_collect_records_http_failure_without_leaking_body():
    settings = _settings()
    client = _FakeClient(_vault_router, status_code=500)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    assert outcome.items == []
    assert outcome.errors and "vault list failed" in outcome.errors[0]
    # The raised error carries status code + path only — never the body.
    assert "nope" not in outcome.errors[0]
    assert "500" in outcome.errors[0]


def test_per_secret_metadata_failure_degrades_to_existence_only():
    settings = _settings()

    def _flaky(url, params):
        if params and params.get("list") == "true":
            return VAULT_LIST_BODY
        raise RuntimeError("metadata read blew up")

    client = _FakeClient(_flaky)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    # Both secrets are still recorded as EXISTING; rotation age is just unknown.
    assert len(outcome.items) == 2
    assert outcome.items[0]["data"]["last_rotated_at"] is None
    assert outcome.errors


# --- REGRESSION: the metadata-only safety boundary ---------------------------


def test_secret_record_has_no_value_shaped_column():
    columns = tuple(c.name for c in SecretRecord.__table__.columns)
    assert set(columns) == set(SECRET_RECORD_COLUMNS), (
        "SecretRecord's column set changed — re-read the safety contract in "
        "db/models/secrets_inventory.py before widening it"
    )
    offenders = [c for c in columns if _VALUE_SHAPED.search(c)]
    assert offenders == [], f"value-shaped column(s) on SecretRecord: {offenders}"


def test_secret_record_cannot_carry_a_value_attribute():
    """The model has no value field to set — not even a hash or ciphertext."""
    for attr in ("value", "secret_value", "encrypted_value", "value_hash", "ciphertext"):
        assert not hasattr(SecretRecord, attr)
    with pytest.raises(TypeError):
        SecretRecord(backend="vault", secret_path="kv/x", value="hunter2")


def test_collector_structured_output_cannot_carry_a_value():
    """The tool's structured output keys are a closed, metadata-only set."""
    settings = _settings()
    client = _FakeClient(_vault_router)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    allowed = {"backend", "secret_path", "last_rotated_at", "referencing_system"}
    for item in outcome.items:
        assert set(item["data"]) == allowed
        assert not [k for k in item["data"] if _VALUE_SHAPED.search(k)]
        # And no persisted field is value-shaped in the DB either: the item's
        # data keys are exactly the SecretRecord columns the writer sets.
        assert allowed.issubset(set(SECRET_RECORD_COLUMNS))


@pytest.mark.parametrize(
    "url",
    [
        "https://vault.example/v1/kv/data/apps/db-dsn",  # Vault KV-v2 VALUE endpoint
        "https://vault.example/v1/secret/data/foo",
        "https://vault.example/v1/cubbyhole/foo",
        "https://bw.example/api/secrets/11111111-1111-1111-1111-111111111111",
        "https://bw.example/api/organizations/org-1/secrets/abc",
        "https://bw.example/anything/else",
        # Traversal: server-side normalization would land on /v1/kv/data/foo.
        "https://vault.example/v1/kv/metadata/apps/../../data/foo",
        "https://vault.example/v1/kv/metadata/apps/%2e%2e/%2e%2e/data/foo",
        "https://vault.example/v1/kv/metadata/apps/..%2f..%2fdata/foo",
        "https://vault.example/v1/kv/metadata/apps/.\\..\\data/foo",
    ],
)
def test_allow_list_refuses_every_non_metadata_endpoint(url):
    with pytest.raises(SecretValueEndpointError):
        _assert_metadata_only(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://vault.example/v1/kv/metadata/apps",
        "https://vault.example/v1/kv/metadata/apps/db-dsn",
        # A secret NAMED "data" nested under the metadata slot is still metadata.
        "https://vault.example/v1/kv/metadata/data/db-dsn",
        "https://bw.example/api/organizations/org-1/secrets",
    ],
)
def test_allow_list_permits_metadata_endpoints(url):
    _assert_metadata_only(url)


def test_upstream_supplied_traversal_key_never_reaches_a_value_endpoint():
    """A hostile/odd secret NAME from the upstream LIST must not escape /metadata/."""
    settings = _settings()
    hostile_list = {"data": {"keys": ["../../data/db-dsn"]}}

    def _router(url, params):
        if params and params.get("list") == "true":
            return hostile_list
        return VAULT_META_BODY

    client = _FakeClient(_router)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()

    # The metadata read for that key was refused; nothing reached a data path.
    assert not any("/data/" in u for u in _FakeClient.requested)
    assert outcome.errors
    assert outcome.items[0]["data"]["last_rotated_at"] is None


def test_tools_refuse_a_tampered_base_url():
    """Even if configuration points the collector at a value path, it refuses."""
    settings = _settings(secrets_inventory_url="https://vault.example/v1/kv/data/x/..")
    client = _FakeClient(_vault_router)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
        pytest.raises(SecretValueEndpointError),
    ):
        vault_list_secret_names_tool.func("kv/apps")
    assert _FakeClient.requested == []


def test_module_source_constructs_no_value_endpoint():
    """No code path — defensive or otherwise — builds a value-returning URL.

    Comments and docstrings are stripped first: the module's prose legitimately
    NAMES the forbidden endpoints in order to explain why they are refused. What
    must not exist is executable code that builds one.
    """
    import io
    import tokenize

    import infra_brain.agents.secrets_inventory as mod

    code_tokens: list[str] = []
    with open(mod.__file__, encoding="utf-8") as fh:
        for tok in tokenize.generate_tokens(io.StringIO(fh.read()).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                # String literals are kept ONLY when they look like URL
                # fragments — that is exactly what we need to inspect — but
                # multi-line (docstring) literals are dropped.
                if "\n" in tok.string:
                    continue
            code_tokens.append(tok.string)
    code = " ".join(code_tokens)

    for forbidden in ("/data/", "GetSecretValue", "get_secret_value", "/api/secrets/"):
        assert forbidden not in code, f"value-returning endpoint fragment in code: {forbidden}"
    # The only URL-building literals present are metadata ones.
    assert "/metadata" in code


def test_bitwarden_projection_drops_unknown_fields():
    """A hypothetical extra field in the body never reaches our output."""
    settings = _settings(
        secrets_inventory_backend="bitwarden", secrets_inventory_url="https://bw.example"
    )
    body = {
        "data": [
            {
                "id": "1",
                "key": "K",
                "revisionDate": "2026-06-01T12:00:00Z",
                "projects": [{"id": "p", "name": "proj"}],
                "somethingUnexpected": "leaked?",
            }
        ]
    }
    client = _FakeClient(lambda url, params: body)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        rows = bitwarden_list_secret_metadata_tool.func("org-1")
    assert rows == [
        {"key": "K", "revision_date": "2026-06-01T12:00:00Z", "referencing_system": "proj"}
    ]


def test_max_secrets_cap_truncates_loudly():
    settings = _settings(secrets_inventory_max_secrets=1)
    client = _FakeClient(_vault_router)
    with (
        patch(f"{MODULE}.get_settings", return_value=settings),
        patch(f"{MODULE}.ReadOnlyClient", client),
    ):
        outcome = _agent(settings).collect()
    assert len(outcome.items) == 1
    assert any("cap" in e for e in outcome.errors)


# --- detail write ------------------------------------------------------------


def test_write_secret_records_upserts_metadata_rows(sqlite_engine, session_patcher):
    from sqlalchemy.orm import Session

    from infra_brain.db.models import Resource

    with Session(sqlite_engine) as session:
        res = Resource(
            name="vault:kv/apps/db-dsn",
            domain="secrets_inventory",
            type="secret",
            source="SecretsInventoryAgent",
        )
        session.add(res)
        session.commit()

    agent = _agent(_settings())
    agent._records = [
        {
            "name": "vault:kv/apps/db-dsn",
            "type": "secret",
            "data": {
                "backend": "vault",
                "secret_path": "kv/apps/db-dsn",
                "last_rotated_at": datetime(2026, 6, 1, tzinfo=UTC).isoformat(),
                "referencing_system": "payments-api",
            },
        }
    ]
    with session_patcher(MODULE):
        assert agent._write_secret_records() == 1

    with Session(sqlite_engine) as session:
        rows = session.query(SecretRecord).all()
        assert len(rows) == 1
        assert rows[0].secret_path == "kv/apps/db-dsn"
        assert rows[0].referencing_system == "payments-api"
