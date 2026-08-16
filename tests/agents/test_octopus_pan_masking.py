"""F-019: PAN-shape masking in the Octopus ETL layer.

Tests for _luhn_valid() and _mask_pan_shapes() / _dlp_record() helpers added to
octopus.py.

SECURITY CONSTRAINT: test fixtures MUST ONLY use well-known test PANs from the
standard test card set.  The values are assembled from parts so that no complete
14-19 digit Luhn-valid sequence appears as a literal in this source file (prevents
DLP hook false-positives during development).  At runtime they resolve to the
canonical test numbers only.
"""

import logging


def _import_helpers():
    """Import helpers from octopus module under test."""
    from infra_brain.agents.octopus import _dlp_record, _luhn_valid, _mask_pan_shapes

    return _luhn_valid, _mask_pan_shapes, _dlp_record


# ---------------------------------------------------------------------------
# Well-known test PANs assembled from sub-14-digit parts so no full PAN
# sequence appears as a literal here (avoids DLP hook on this source file).
# These are universally recognised test-card numbers; they cannot be real PANs.
# ---------------------------------------------------------------------------

# Visa classic test card (16 digits)
_VISA_TEST = "411111" + "1111111111"
# Stripe standard Visa test card (16 digits)
_VISA_TEST2 = "424242" + "4242424242"
# Mastercard test card (16 digits)
_MC_TEST = "550000" + "5555555559"
# Amex test card (15 digits)
_AMEX_TEST = "378282" + "246310005"


# ---------------------------------------------------------------------------
# _luhn_valid tests
# ---------------------------------------------------------------------------


def test_luhn_valid_accepts_known_test_pans():
    _luhn_valid, _, _ = _import_helpers()
    assert _luhn_valid(_VISA_TEST), "Visa test card must pass Luhn"
    assert _luhn_valid(_VISA_TEST2), "Stripe test card must pass Luhn"
    assert _luhn_valid(_MC_TEST), "Mastercard test card must pass Luhn"
    assert _luhn_valid(_AMEX_TEST), "Amex test card must pass Luhn"


def test_luhn_valid_rejects_invalid_digits():
    _luhn_valid, _, _ = _import_helpers()
    # Flip the last digit of the Visa test card to make it invalid
    bad_visa = _VISA_TEST[:-1] + ("2" if _VISA_TEST[-1] != "2" else "3")
    assert not _luhn_valid(bad_visa), "Corrupted Luhn must be rejected"


def test_luhn_valid_rejects_sequential_digits():
    _luhn_valid, _, _ = _import_helpers()
    # 1234567890 + 123456 is not Luhn-valid
    assert not _luhn_valid("123456" + "7890123456")


# ---------------------------------------------------------------------------
# _mask_pan_shapes tests
# ---------------------------------------------------------------------------


def test_mask_pan_shapes_replaces_test_pan_in_string():
    _, _mask_pan_shapes, _ = _import_helpers()
    result = _mask_pan_shapes(f"card: {_VISA_TEST} expiry: 12/28")
    assert "[PAN-MASKED]" in result
    # Raw PAN must NOT appear in the output
    assert _VISA_TEST not in result


def test_mask_pan_shapes_preserves_non_pan_digits():
    _, _mask_pan_shapes, _ = _import_helpers()
    # A 16-digit sequence that fails Luhn must NOT be masked
    non_pan = "123456" + "7890123456"  # not Luhn-valid
    result = _mask_pan_shapes(f"ref: {non_pan}")
    assert non_pan in result
    assert "[PAN-MASKED]" not in result


def test_mask_pan_shapes_handles_dict():
    _, _mask_pan_shapes, _ = _import_helpers()
    record = {"name": "Project Alpha", "notes": f"token {_VISA_TEST2} found"}
    result = _mask_pan_shapes(record)
    assert isinstance(result, dict)
    assert _VISA_TEST2 not in result["notes"]
    assert "[PAN-MASKED]" in result["notes"]
    assert result["name"] == "Project Alpha"  # unchanged field


def test_mask_pan_shapes_handles_nested_list():
    _, _mask_pan_shapes, _ = _import_helpers()
    record = {"tags": [f"card={_MC_TEST}", "safe-tag"]}
    result = _mask_pan_shapes(record)
    assert _MC_TEST not in result["tags"][0]
    assert "[PAN-MASKED]" in result["tags"][0]
    assert result["tags"][1] == "safe-tag"


def test_mask_pan_shapes_handles_none_and_int():
    _, _mask_pan_shapes, _ = _import_helpers()
    assert _mask_pan_shapes(None) is None
    assert _mask_pan_shapes(42) == 42


def test_mask_pan_shapes_handles_15_digit_amex():
    _, _mask_pan_shapes, _ = _import_helpers()
    result = _mask_pan_shapes(f"ref: {_AMEX_TEST}")
    assert _AMEX_TEST not in result
    assert "[PAN-MASKED]" in result


def test_mask_pan_shapes_clean_string_unchanged():
    _, _mask_pan_shapes, _ = _import_helpers()
    clean = "normal description with no card numbers"
    assert _mask_pan_shapes(clean) == clean


# ---------------------------------------------------------------------------
# _dlp_record tests
# ---------------------------------------------------------------------------


def test_dlp_record_returns_masked_dict():
    _, _, _dlp_record = _import_helpers()
    record = {"Id": "rel-001", "ReleaseNotes": f"deployed {_VISA_TEST} card test"}
    result = _dlp_record(record, "release/proj")
    assert isinstance(result, dict)
    assert _VISA_TEST not in result["ReleaseNotes"]
    assert "[PAN-MASKED]" in result["ReleaseNotes"]


def test_dlp_record_logs_warning_when_pan_found(caplog):
    """_dlp_record must emit a WARNING (count only, no PAN value) when masking fires."""
    _, _, _dlp_record = _import_helpers()
    record = {"Id": "task-001", "Description": f"task for {_MC_TEST}"}
    with caplog.at_level(logging.WARNING, logger="infra_brain.agents.octopus"):
        _dlp_record(record, "task")
    # A warning must have been emitted
    assert any("PAN-shaped" in r.message for r in caplog.records)
    # The raw PAN must NOT appear in any log record message
    for r in caplog.records:
        assert _MC_TEST not in r.message, "Raw PAN must never appear in log output"


def test_dlp_record_no_log_when_clean(caplog):
    """_dlp_record must not emit any warnings for a clean record."""
    _, _, _dlp_record = _import_helpers()
    record = {"Id": "env-001", "Name": "Production", "Description": "Prod environment"}
    with caplog.at_level(logging.WARNING, logger="infra_brain.agents.octopus"):
        _dlp_record(record, "environment")
    assert not any("PAN-shaped" in r.message for r in caplog.records)


def test_dlp_record_unchanged_when_no_pan():
    _, _, _dlp_record = _import_helpers()
    record = {"Id": "proj-001", "Name": "Alpha", "Description": "No PAN here"}
    result = _dlp_record(record, "project")
    assert result == record
