"""Structural safety test (GitLab #103): the SaaS API-key metadata table must
have NO column that could ever hold the key value/secret.
"""

from infra_brain.db.models import SaaSApiKeyMetadata, SaaSApplication


def test_saas_application_tablename():
    assert SaaSApplication.__tablename__ == "saas_applications"


def test_saas_api_key_metadata_tablename():
    assert SaaSApiKeyMetadata.__tablename__ == "saas_api_key_metadata"


def test_saas_api_key_metadata_has_expected_metadata_columns():
    columns = {c.name for c in SaaSApiKeyMetadata.__table__.columns}
    for expected in ("key_name", "scope", "created_at", "last_used_at"):
        assert expected in columns


def test_saas_api_key_metadata_has_no_secret_column():
    """Structural enforcement: no column name could hold the key value."""
    columns = {c.name.lower() for c in SaaSApiKeyMetadata.__table__.columns}
    forbidden = {"value", "secret", "key_value", "api_key_value", "token"}
    assert not (columns & forbidden), (
        f"forbidden secret-shaped column(s) found: {columns & forbidden}"
    )
