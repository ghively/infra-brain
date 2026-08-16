"""Secrets-manager INVENTORY (metadata only) — GitLab issue #98.

SAFETY CONTRACT (read this before adding any column)
====================================================
This table records that a secret EXISTS, when it was last rotated, and which
system/service-account references it. It must NEVER record what the secret IS.

There is deliberately:
  * no ``value`` column,
  * no ``encrypted_value`` / ``ciphertext`` / ``sealed_value`` column,
  * no ``value_hash`` / ``digest`` / ``fingerprint`` column — a hash is still a
    verifiable oracle for a low-entropy secret and would turn this table into a
    cracking target, so "just a hash" is NOT an acceptable relaxation,
  * no free-form JSONB blob — every other collector's detail tables carry one,
    and that is exactly the column into which a future "let's just stash the raw
    API response for debugging" change would drop a secret value. The columns
    here are a closed, typed set on purpose.

``SecretRecord`` is the ONLY persistence surface for
``infra_brain.agents.secrets_inventory``. The collector that populates it never
calls a value-returning endpoint (see that module's ``_assert_metadata_only``
allow-list), so there is no value in the process to accidentally persist —
this table's shape is the second, independent layer of that same guarantee.

If a future requirement genuinely needs secret material, it does not belong in
infra-brain at all: infra-brain is read-only audit/documentation infrastructure
(docs/READONLY-MODEL.md), and holding secret material would make its database a
higher-value target than the secrets managers it inventories.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid

# Backend discriminator values written to ``SecretRecord.backend``.
BACKEND_VAULT = "vault"
BACKEND_BITWARDEN = "bitwarden"
BACKEND_AWS = "aws"

# Every column name this model is permitted to carry. The regression test in
# tests/agents/test_secrets_inventory.py asserts the mapped columns are exactly
# this set, so ADDING A COLUMN IS A TEST FAILURE until someone updates this
# tuple — which forces the safety contract above to be re-read rather than
# silently widened.
SECRET_RECORD_COLUMNS: tuple[str, ...] = (
    "id",
    "resource_id",
    "backend",
    "secret_path",
    "last_rotated_at",
    "referencing_system",
    "updated_at",
)


class SecretRecord(Base):
    """One row per secret KNOWN TO EXIST in an external secrets manager.

    Metadata only — see the module docstring's safety contract.
    """

    __tablename__ = "secret_records"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id"), nullable=False, index=True
    )
    # "vault" | "bitwarden" | "aws" — which secrets manager this row came from.
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    # The secret's PATH/NAME within that backend (e.g. "kv/data-pipeline/db").
    # A path/name is an identifier, not secret material; it is what an operator
    # needs in order to go rotate the thing in the backend's own UI.
    secret_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # Rotation age source: Vault KV-v2 metadata ``updated_time`` / Bitwarden
    # Secrets Manager ``revisionDate``. NULL when the backend did not report one.
    last_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which system / service-account references this secret, as declared in the
    # backend's own METADATA (Vault custom_metadata, Bitwarden project name) —
    # never inferred from, and never containing, the secret's value.
    referencing_system: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    __table_args__ = (
        UniqueConstraint("backend", "secret_path", name="uq_secret_records_backend_path"),
    )


__all__ = [
    "BACKEND_AWS",
    "BACKEND_BITWARDEN",
    "BACKEND_VAULT",
    "SECRET_RECORD_COLUMNS",
    "SecretRecord",
]
