"""Shared mask-at-ingestion PAN scrubbing (infra_brain.etl.masking).

Generalized out of agents/octopus.py so any collector — not just Octopus —
can scrub Luhn-valid card-number shapes from raw ingested payloads. See
tests/agents/test_octopus_pan_masking.py for the octopus.py wrapper's own
regression coverage (log-namespace preservation etc.); this module tests the
shared implementation directly, including the parts a second, non-Octopus
collector would exercise (default logger, ``source`` label).
"""

import logging

from infra_brain.etl.masking import PAN_MASK_TOKEN, dlp_scrub, luhn_valid, mask_pan_shapes

# Well-known test PANs, assembled from sub-14-digit parts so no full PAN
# sequence appears as a literal here (avoids DLP hook false-positives).
_VISA_TEST = "411111" + "1111111111"
_MC_TEST = "550000" + "5555555559"


def test_luhn_valid_accepts_known_test_pan():
    assert luhn_valid(_VISA_TEST)


def test_luhn_valid_rejects_bad_digits():
    bad = _VISA_TEST[:-1] + ("2" if _VISA_TEST[-1] != "2" else "3")
    assert not luhn_valid(bad)


def test_mask_pan_shapes_replaces_valid_pan():
    result = mask_pan_shapes(f"card: {_VISA_TEST}")
    assert _VISA_TEST not in result
    assert PAN_MASK_TOKEN in result


def test_mask_pan_shapes_replaces_space_separated_pan():
    """F-019: separator-form PANs (as the module docstring/comment claims is
    handled) must be masked, not passed through unmasked."""
    spaced = " ".join(_VISA_TEST[i : i + 4] for i in range(0, len(_VISA_TEST), 4))
    result = mask_pan_shapes(f"card: {spaced}")
    assert spaced not in result
    assert PAN_MASK_TOKEN in result


def test_mask_pan_shapes_replaces_hyphen_separated_pan():
    hyphenated = "-".join(_MC_TEST[i : i + 4] for i in range(0, len(_MC_TEST), 4))
    result = mask_pan_shapes(f"card: {hyphenated}")
    assert hyphenated not in result
    assert PAN_MASK_TOKEN in result


def test_mask_pan_shapes_nested_structures():
    record = {"notes": [f"ref {_MC_TEST}", "clean"]}
    result = mask_pan_shapes(record)
    assert _MC_TEST not in result["notes"][0]
    assert result["notes"][1] == "clean"


def test_dlp_scrub_uses_caller_supplied_logger(caplog):
    """A collector other than Octopus can pass its OWN logger so the warning is
    attributed to that collector's module, not infra_brain.etl.masking."""
    other_log = logging.getLogger("infra_brain.agents.some_other_collector")
    record = {"description": f"found {_VISA_TEST}"}
    with caplog.at_level(logging.WARNING, logger="infra_brain.agents.some_other_collector"):
        result = dlp_scrub(record, "some-context", log=other_log, source="some_other_collector")
    assert _VISA_TEST not in result["description"]
    assert any(
        r.name == "infra_brain.agents.some_other_collector" and "PAN-shaped" in r.message
        for r in caplog.records
    )
    # The raw PAN must never appear in any log record message.
    for r in caplog.records:
        assert _VISA_TEST not in r.message


def test_dlp_scrub_defaults_to_module_logger_when_none_supplied(caplog):
    record = {"description": f"found {_MC_TEST}"}
    with caplog.at_level(logging.WARNING, logger="infra_brain.etl.masking"):
        dlp_scrub(record)
    assert any("PAN-shaped" in r.message for r in caplog.records)


def test_dlp_scrub_no_warning_for_clean_record(caplog):
    other_log = logging.getLogger("infra_brain.agents.some_other_collector")
    with caplog.at_level(logging.WARNING, logger="infra_brain.agents.some_other_collector"):
        result = dlp_scrub({"name": "clean record"}, log=other_log)
    assert result == {"name": "clean record"}
    assert not any("PAN-shaped" in r.message for r in caplog.records)
