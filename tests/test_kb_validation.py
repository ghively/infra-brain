"""Tests for kb_validation.py — TRK-193 sub-bug 2.

RemediationAgent's LLM-drafted plan text had no validation layer on any KB
(Microsoft Knowledge Base) article number the model chose to write into a
recommendation. These tests cover the format-validation layer added to close
that gap: well-formed KB IDs pass through untouched, malformed ones are
flagged inline rather than silently passed through.
"""

from infra_brain.kb_validation import flag_invalid_kb_references, is_valid_kb_id


# --- is_valid_kb_id -----------------------------------------------------------


def test_valid_kb_id_seven_digits():
    assert is_valid_kb_id("KB5034122") is True


def test_valid_kb_id_six_digits():
    assert is_valid_kb_id("KB925673") is True


def test_invalid_kb_id_wrong_digit_count_too_short():
    assert is_valid_kb_id("KB123") is False


def test_invalid_kb_id_wrong_digit_count_too_long():
    assert is_valid_kb_id("KB123456789") is False


def test_invalid_kb_id_letters_in_digit_run():
    assert is_valid_kb_id("KB50341AB") is False


def test_invalid_kb_id_separator_not_allowed():
    assert is_valid_kb_id("KB-5034122") is False
    assert is_valid_kb_id("KB 5034122") is False


def test_invalid_kb_id_not_kb_shaped_at_all():
    assert is_valid_kb_id("Rocky Linux 9") is False
    assert is_valid_kb_id("") is False


# --- flag_invalid_kb_references ------------------------------------------------


def test_flag_invalid_kb_references_passes_through_well_formed():
    text = "Apply KB5034122 to remediate the cumulative update drift."
    annotated, flagged = flag_invalid_kb_references(text)
    assert annotated == text
    assert flagged == []


def test_flag_invalid_kb_references_flags_malformed_id():
    text = "Apply KB123 to remediate the drift."
    annotated, flagged = flag_invalid_kb_references(text)
    assert flagged == ["KB123"]
    assert "KB123" in annotated
    assert "UNVERIFIED" in annotated
    assert "not a valid KB-article-ID format" in annotated


def test_flag_invalid_kb_references_flags_separator_variant():
    text = "See KB-503412 for details."
    annotated, flagged = flag_invalid_kb_references(text)
    assert flagged == ["KB-503412"]
    assert "UNVERIFIED" in annotated


def test_flag_invalid_kb_references_no_kb_mentions_is_unchanged():
    text = "Reconcile the drifted `kernel` field back to the expected value."
    annotated, flagged = flag_invalid_kb_references(text)
    assert annotated == text
    assert flagged == []


def test_flag_invalid_kb_references_mixed_valid_and_invalid():
    text = "Primary fix is KB5034122; also consider KB99 as a workaround."
    annotated, flagged = flag_invalid_kb_references(text)
    assert flagged == ["KB99"]
    assert "KB5034122" in annotated
    assert "KB5034122 [UNVERIFIED" not in annotated
    assert "KB99 [UNVERIFIED" in annotated


def test_flag_invalid_kb_references_case_insensitive_candidate_match():
    text = "apply kb123 now"
    annotated, flagged = flag_invalid_kb_references(text)
    assert flagged == ["kb123"]
