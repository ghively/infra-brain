"""RuntimeConfig model round-trip tests (TRK-303 part 1/7)."""

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from infra_brain.db.models import RuntimeConfig

from tests.support.pg import make_engine


def test_runtime_config_round_trip():
    eng = make_engine()
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="netdiscovery_tier2_chunk_size",
                value_type="int",
                value="500",
                category="tuning",
            )
        )
        s.commit()
        row = s.get(RuntimeConfig, "netdiscovery_tier2_chunk_size")
        assert row.value == "500"
        assert row.is_secret is False
        assert row.updated_at is not None
        assert row.updated_by == ""


def test_runtime_config_secret_row_stores_encrypted_bytes():
    eng = make_engine()
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="vsphere_password",
                value_type="str",
                value=None,
                encrypted_value=b"\x00\x01ciphertext",
                is_secret=True,
                category="secret",
                updated_by="admin@example.com",
            )
        )
        s.commit()
        row = s.get(RuntimeConfig, "vsphere_password")
        assert row.value is None
        assert row.encrypted_value == b"\x00\x01ciphertext"
        assert row.is_secret is True
        assert row.category == "secret"
        assert row.updated_by == "admin@example.com"


def test_runtime_config_schema_columns_match_spec():
    """Later TRK-303 tasks reference these column names exactly."""
    eng = make_engine()
    cols = {c["name"] for c in inspect(eng).get_columns("runtime_config")}
    assert cols == {
        "key",
        "value_type",
        "value",
        "encrypted_value",
        "is_secret",
        "category",
        "updated_by",
        "updated_at",
    }
    assert RuntimeConfig.__tablename__ == "runtime_config"
