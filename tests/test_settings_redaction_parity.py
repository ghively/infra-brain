"""TRK-342 — the TWO full-settings surfaces must share ONE redactor.

There are two places in this codebase that dump the ENTIRE ``Settings`` model
to a caller:

  * ``GET /api/dashboard/settings`` (admin-gated, TRK-321) → renders each field
    through ``infra_brain.api._helpers._setting_row``.
  * the ``get_settings`` MCP tool (token-gated) →
    ``infra_brain.mcp_server.get_settings``.

Before TRK-342 each carried its OWN copy of the redaction rule: the same
``_SECRET_HINTS`` name check, but two DIFFERENT value-shaped DSN passes
(``etl.base.scrub_dsn`` on one side, a locally-defined ``_DSN_CREDENTIAL_RE``
plus whole-value ``mask_secret`` on the other). Two copies of a security rule
drift: a new sensitive field gets masked on the surface whoever wrote it was
looking at, and stays cleartext on the other.

These tests pin the invariant that makes that impossible, in two layers:

  1. BEHAVIOURAL PARITY (``TestMaskedFieldSetParity``) — feed BOTH surfaces the
     identical field/value matrix and assert they mask the *same set of
     fields*. Driven off the live ``Settings`` model's own field list, so a
     field added tomorrow is covered without editing this file.

  2. STRUCTURAL ANTI-DRIFT (``TestNeitherSurfaceCarriesItsOwnMaskingLogic``) —
     AST-inspect both functions and fail if EITHER of them names a masking
     primitive directly. This is the layer that fails when someone "adds
     masking to one side only": the only way to add a rule is to add it to the
     shared helper, which by construction changes both surfaces at once.
     Layer 1 alone would not catch it — a rule added identically-by-accident to
     both copies still passes a parity check while leaving two copies to drift
     next time.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any
from unittest.mock import patch

from infra_brain import mcp_server
from infra_brain.api._helpers import _setting_row
from infra_brain.config import Settings

#: Never a real credential — a distinctive byte-string we assert never survives.
PROBE_PASSWORD = "trk342ProbePassword"  # noqa: S105 - test fixture
#: A DSN-shaped value. Every ``Settings`` field is probed with this so the
#: parity check does not depend on any particular field's real default.
PROBE_DSN = f"postgresql+psycopg://probeuser:{PROBE_PASSWORD}@db.internal:5432/infra_brain"


def _mcp_settings_dump(fields: dict[str, Any]) -> dict[str, Any]:
    """Run the MCP surface over an injected field map."""

    class _FakeSettings:
        def model_dump(self) -> dict[str, Any]:
            return dict(fields)

    with patch("infra_brain.config.get_settings", return_value=_FakeSettings()):
        return mcp_server.get_settings()


def _settings_field_names() -> list[str]:
    """Every field on the live Settings model — the surface both dumps cover."""
    return sorted(Settings.model_fields)


def _dashboard_masked(key: str, value: Any) -> bool:
    """Did the dashboard surface redact this (key, value)?"""
    row = _setting_row(key, value)
    if row.type == "bool":
        return False
    rendered = row.v or ""
    return rendered != str(value)


def _mcp_masked(key: str, value: Any, dumped: dict[str, Any]) -> bool:
    """Did the MCP surface redact this (key, value)?"""
    if isinstance(value, bool):
        return False
    return str(dumped[key]) != str(value)


class TestMaskedFieldSetParity:
    """The two surfaces must agree, field for field, on what gets redacted."""

    def test_every_settings_field_is_masked_identically_for_a_dsn_value(self):
        """Drive every real Settings field with a credential-bearing value.

        This is the "same field set" pin. It is deliberately driven off
        ``Settings.model_fields`` rather than a hardcoded list, so a field added
        after this test was written is checked automatically.
        """
        fields = {key: PROBE_DSN for key in _settings_field_names()}
        dumped = _mcp_settings_dump(fields)

        dashboard_masked = {k for k, v in fields.items() if _dashboard_masked(k, v)}
        mcp_masked = {k for k, v in fields.items() if _mcp_masked(k, v, dumped)}

        assert dashboard_masked == mcp_masked, (
            "the dashboard and MCP settings surfaces disagree on which fields "
            "are redacted — they must share one redactor.\n"
            f"  masked on dashboard only: {sorted(dashboard_masked - mcp_masked)}\n"
            f"  masked on MCP only:       {sorted(mcp_masked - dashboard_masked)}"
        )
        # Sanity: the probe value carries a credential, so EVERY field must be
        # masked. A parity check over two empty sets would pass vacuously.
        assert dashboard_masked == set(fields), (
            "a credential-bearing value survived unredacted on the dashboard "
            f"surface for: {sorted(set(fields) - dashboard_masked)}"
        )

    def test_no_settings_field_leaks_the_probe_credential_on_either_surface(self):
        fields = {key: PROBE_DSN for key in _settings_field_names()}
        dumped = _mcp_settings_dump(fields)
        for key in fields:
            assert PROBE_PASSWORD not in str(dumped[key]), f"MCP leaked {key}"
            assert PROBE_PASSWORD not in (_setting_row(key, PROBE_DSN).v or ""), (
                f"dashboard leaked {key}"
            )

    def test_secret_hinted_names_are_masked_identically_without_a_dsn(self):
        """Name-hint masking (the `_SECRET_HINTS` path) must also agree.

        Uses a plain opaque token, so only the NAME can trigger redaction —
        this isolates the name-hint rule from the DSN value rule above.
        """
        fields = {key: "plainOpaqueValue1234" for key in _settings_field_names()}
        dumped = _mcp_settings_dump(fields)

        dashboard_masked = {k for k, v in fields.items() if _dashboard_masked(k, v)}
        mcp_masked = {k for k, v in fields.items() if _mcp_masked(k, v, dumped)}
        assert dashboard_masked == mcp_masked
        # And it is not vacuous: this project has secret-hinted fields.
        assert dashboard_masked, "expected at least one secret-hinted Settings field"

    def test_rendered_value_is_identical_not_merely_both_masked(self):
        """Same field set is necessary but not sufficient — the two surfaces
        must also redact to the SAME rendering, or an operator comparing the
        dashboard against an agent's MCP read sees two different answers."""
        fields = {key: PROBE_DSN for key in _settings_field_names()}
        dumped = _mcp_settings_dump(fields)
        for key in fields:
            assert str(dumped[key]) == (_setting_row(key, PROBE_DSN).v or ""), (
                f"{key} renders differently on the two settings surfaces"
            )

    def test_non_credential_values_are_untouched_on_both_surfaces(self):
        """Over-redaction is its own failure — a credential-free URL must stay
        readable on BOTH surfaces so operators can see what is configured."""
        plain = "https://gitlab.example.com"
        dumped = _mcp_settings_dump({"gitlab_url": plain})
        assert dumped["gitlab_url"] == plain
        assert _setting_row("gitlab_url", plain).v == plain

    def test_bool_fields_are_not_masked_on_either_surface(self):
        dumped = _mcp_settings_dump({"dlp_fail_closed": True})
        assert dumped["dlp_fail_closed"] is True
        assert _setting_row("dlp_fail_closed", True).type == "bool"

    def test_mcp_preserves_json_type_for_untouched_non_string_values(self):
        """The MCP surface is machine-facing: an int must stay an int when
        nothing was redacted, rather than being stringified by the shared
        helper. (The dashboard renders everything as text by contract; that
        difference is presentational, not a redaction difference.)"""
        dumped = _mcp_settings_dump({"llm_timeout_seconds": 30, "llm_provider": "anthropic"})
        assert dumped["llm_timeout_seconds"] == 30
        assert isinstance(dumped["llm_timeout_seconds"], int)


class TestNeitherSurfaceCarriesItsOwnMaskingLogic:
    """The layer that fails when masking is added to ONE side only.

    Parity alone cannot enforce "one implementation" — two identical copies
    pass it. These tests read the two functions' source and refuse any direct
    reference to a masking primitive, so the only place a rule CAN be added is
    the shared helper.
    """

    #: Naming any of these inside a settings-dump function means that function
    #: is implementing redaction itself rather than delegating.
    FORBIDDEN = {
        "_SECRET_HINTS",
        "mask_secret",
        "scrub_dsn",
        "_DSN_CREDENTIAL_RE",
        "_DSN_CREDENTIALS_RE",
    }
    #: The one shared entry point both surfaces must go through.
    REQUIRED = "redact_setting"

    @staticmethod
    def _names_used(func) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                used.update(alias.asname or alias.name for alias in node.names)
        return used

    def test_dashboard_setting_row_delegates_all_masking(self):
        used = self._names_used(_setting_row)
        leaked = used & self.FORBIDDEN
        assert not leaked, (
            f"_setting_row implements its own masking ({sorted(leaked)}) instead of "
            f"delegating to {self.REQUIRED}() — the second copy TRK-342 removed"
        )
        assert self.REQUIRED in used, f"_setting_row must call {self.REQUIRED}()"

    def test_mcp_get_settings_delegates_all_masking(self):
        used = self._names_used(mcp_server.get_settings)
        leaked = used & self.FORBIDDEN
        assert not leaked, (
            f"mcp_server.get_settings implements its own masking ({sorted(leaked)}) "
            f"instead of delegating to {self.REQUIRED}() — the exact divergence "
            "TRK-342 exists to prevent"
        )
        assert self.REQUIRED in used, f"mcp_server.get_settings must call {self.REQUIRED}()"

    def test_the_shared_helper_is_the_one_that_owns_the_rules(self):
        """Positive control: the primitives must live SOMEWHERE, and that
        somewhere is the shared helper. Without this, deleting all masking
        everywhere would pass the two tests above."""
        from infra_brain.api._helpers import redact_setting

        used = self._names_used(redact_setting)
        assert "_SECRET_HINTS" in used
        assert "mask_secret" in used
        assert "scrub_dsn" in used
