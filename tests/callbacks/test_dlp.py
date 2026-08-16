import uuid
import pytest
from unittest.mock import patch, MagicMock
from infra_brain.callbacks.dlp import (
    DLPCallbackHandler,
    is_probable_pan,
    luhn_check,
    redact_pans,
    redact_pans_preserving_uuids,
)

# A real uuid4 (found by brute force, TRK-346) whose first three groups —
# 8+4+4 = 16 hyphen-separated characters — are all digits, Luhn-valid, and in
# Mastercard's 51-55 IIN range, so _PAN_RE + is_probable_pan classify the middle
# of the identifier as a card number. ~1 random uuid4 in 40,000 looks like this.
PAN_SHAPED_UUID = "52307540-7105-4335-a4c6-9ed9ec3fbf05"


def test_luhn_check_valid():
    assert luhn_check("4111111111111111") is True


def test_luhn_check_invalid():
    assert luhn_check("1234567890123456") is False


def test_pan_in_output_raises_when_fail_closed():
    h = DLPCallbackHandler(agent_name="TestAgent")
    mock_session = MagicMock()
    with (
        patch("infra_brain.callbacks.dlp.get_settings") as mock_settings,
        patch("infra_brain.callbacks.dlp.get_session") as mock_get_session,
    ):
        mock_settings.return_value.dlp_fail_closed = True
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError, match="PAN"):
            h.on_tool_end(
                "Found card: 4111111111111111 in config",
                run_id=uuid.uuid4(),
            )


def test_no_pan_passes():
    h = DLPCallbackHandler(agent_name="TestAgent")
    mock_session = MagicMock()
    with (
        patch("infra_brain.callbacks.dlp.get_settings") as mock_settings,
        patch("infra_brain.callbacks.dlp.get_session") as mock_get_session,
    ):
        mock_settings.return_value.dlp_fail_closed = True
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        # Should not raise
        h.on_tool_end("hostname: web01.example.com", run_id=uuid.uuid4())


def test_redact_pans_masks_valid_card():
    """A Luhn-valid PAN in text is replaced; the digits do not survive."""
    out = redact_pans("Card on file: 4111111111111111 please charge it")
    assert "4111111111111111" not in out
    assert "REDACTED" in out


def test_redact_pans_leaves_non_pan_untouched():
    """A non-Luhn digit run (e.g. an order id) is not masked."""
    out = redact_pans("order 1234567890123456 shipped")
    assert "1234567890123456" in out


def _make_luhn_valid(body: str) -> str:
    """Append the single check digit that makes ``body`` Luhn-valid — lets us
    build Luhn-passing NON-card identifiers to prove the IIN check (not Luhn) is
    what rejects them."""
    for d in "0123456789":
        if luhn_check(body + d):
            return body + d
    raise AssertionError("no Luhn-completing digit")  # pragma: no cover


# Real payment-card test PANs (all Luhn-valid) — every network must still fire.
@pytest.mark.parametrize(
    "pan",
    [
        "4111111111111111",  # Visa
        "378282246310005",  # Amex
        "5555555555554444",  # Mastercard
        "6011111111111117",  # Discover
        "3530111333300000",  # JCB
        "30569309025904",  # Diners Club
    ],
)
def test_is_probable_pan_true_for_real_cards(pan):
    assert luhn_check(pan) is True
    assert is_probable_pan(pan) is True


# Luhn-valid infra identifiers that are NOT cards — the false positives that
# were killing the vuln sweep (cert serials prefix 58, Rapid7 agent IDs prefix
# 00). They pass Luhn but match no card IIN, so must NOT be treated as PANs.
def test_is_probable_pan_false_for_luhn_valid_non_cards():
    agent_id = _make_luhn_valid("000000000000000")  # 16 digits, prefix "00"
    cert_serial = _make_luhn_valid("580000000000000000")  # 19 digits, prefix "58"
    assert luhn_check(agent_id) is True and luhn_check(cert_serial) is True
    assert is_probable_pan(agent_id) is False
    assert is_probable_pan(cert_serial) is False


def test_maestro_exclusion_is_a_locked_decision():
    """DELIBERATE COVERAGE TRADE-OFF (lc-safety-reviewer 2026-07-13): Maestro
    (BIN 50, 56-69 — including the '58' prefix shared with 19-digit X.509 cert
    serials) and RuPay are KNOWINGLY excluded from _matches_card_network so the
    vuln sweep is not re-broken by cert-serial false positives. This test locks
    that decision — if someone adds Maestro coverage, they must consciously
    update this test and re-accept the false-positive risk.
    """
    maestro = _make_luhn_valid("501800000000000")  # 16-digit Maestro (BIN 5018)
    assert luhn_check(maestro) is True
    # Knowingly NOT treated as a PAN in this system.
    assert is_probable_pan(maestro) is False


def test_luhn_valid_non_card_does_not_raise_dlp():
    """A Luhn-valid Rapid7 agent-id shape in tool output must NOT trip DLP."""
    agent_id = _make_luhn_valid("000000000000000")
    h = DLPCallbackHandler(agent_name="VulnAgent")
    with (
        patch("infra_brain.callbacks.dlp.get_settings") as mock_settings,
        patch("infra_brain.callbacks.dlp.get_session"),
    ):
        mock_settings.return_value.dlp_fail_closed = True
        # Must not raise despite being Luhn-valid.
        h.on_tool_end(f'{{"id": {agent_id}, "source": "R7 Agent"}}', run_id=uuid.uuid4())


def test_redact_pans_leaves_luhn_valid_non_card_untouched():
    cert_serial = _make_luhn_valid("580000000000000000")
    out = redact_pans(f'"cert.serial.number": "{cert_serial}"')
    assert cert_serial in out
    assert "REDACTED" not in out


def test_redact_pans_does_mangle_a_pan_shaped_uuid():
    """Documents the trap the preserving variant exists for — not a wish.

    This asserts CURRENT redact_pans behaviour on purpose: the primitive is a
    safety guard and is deliberately left alone. Callers that serialize row ids
    must use redact_pans_preserving_uuids instead.
    """
    assert redact_pans(PAN_SHAPED_UUID) != PAN_SHAPED_UUID
    assert "REDACTED" in redact_pans(PAN_SHAPED_UUID)


def test_preserving_variant_leaves_a_pan_shaped_uuid_intact():
    assert redact_pans_preserving_uuids(PAN_SHAPED_UUID) == PAN_SHAPED_UUID


def test_preserving_variant_survives_json_round_trip():
    """The TRK-346 shape: an id list inside a serialized audit blob."""
    import json

    ids = [PAN_SHAPED_UUID, str(uuid.uuid4()), PAN_SHAPED_UUID.upper()]
    blob = json.dumps({"resolved_ids": ids, "note": "batch"})
    assert json.loads(redact_pans_preserving_uuids(blob))["resolved_ids"] == ids


def test_preserving_variant_still_redacts_free_text_around_uuids():
    """Exempting UUID tokens must not create a hiding place for a real PAN."""
    text = f'{{"id": "{PAN_SHAPED_UUID}", "note": "card 4111111111111111 on file"}}'
    out = redact_pans_preserving_uuids(text)
    assert PAN_SHAPED_UUID in out
    assert "4111111111111111" not in out
    assert "REDACTED" in out


def test_preserving_variant_matches_redact_pans_when_no_uuid_present():
    for probe in ("card 4111111111111111", "nothing here", "order 1234567890123456", ""):
        assert redact_pans_preserving_uuids(probe) == redact_pans(probe)


def test_on_llm_end_scans_output_raises_when_fail_closed():
    """F-032: on_llm_end must scan model output and raise under dlp_fail_closed."""
    h = DLPCallbackHandler(agent_name="TestAgent")
    mock_session = MagicMock()
    with (
        patch("infra_brain.callbacks.dlp.get_settings") as mock_settings,
        patch("infra_brain.callbacks.dlp.get_session") as mock_get_session,
    ):
        mock_settings.return_value.dlp_fail_closed = True
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError, match="PAN"):
            h.on_llm_end("Your card 4111111111111111 is on file", run_id=uuid.uuid4())


def test_permissive_mode_pan_detection_logs_at_error_not_warning(caplog):
    """#85: dlp_fail_closed=False must not downgrade a detected PAN leak to
    WARNING — the call still proceeds (permissive), but the log must be loud.

    Invoked via the public on_tool_end() entry point, matching this file's
    existing test_pan_in_output_raises_when_fail_closed() convention (same
    call shape, dlp_fail_closed flipped to False)."""
    import logging

    from infra_brain.callbacks.dlp import DLPCallbackHandler

    h = DLPCallbackHandler(agent_name="TestAgent")
    mock_session = MagicMock()
    with (
        patch("infra_brain.callbacks.dlp.get_settings") as mock_settings,
        patch("infra_brain.callbacks.dlp.get_session") as mock_get_session,
        caplog.at_level(logging.WARNING, logger="infra_brain.callbacks.dlp"),
    ):
        mock_settings.return_value.dlp_fail_closed = False
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        # A Luhn-valid test card number (widely used test number, not a real card).
        h.on_tool_end("Found card: 4111111111111111 in config", run_id=uuid.uuid4())

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert error_records, f"expected the permissive-mode PAN detection to log at ERROR; got {[r.levelname for r in caplog.records]}"
    assert not warning_records, "permissive-mode PAN detection must not log at WARNING (#85)"
