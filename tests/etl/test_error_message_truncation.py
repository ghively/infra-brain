"""CollectionRun.error_message is an unbounded Postgres TEXT column (see
db/models/core.py) -- there is no DB-imposed width limit. ``_ERROR_MESSAGE_MAX_CHARS``
is a self-imposed practical cap only, sized generously above realistic
per-domain error volumes so real cases are never truncated; it exists solely
to bound genuinely pathological error counts. A domain with many per-item
errors (e.g. homelab_services: confirmed live at 32 skipped-entry errors,
~300 chars each = ~9800 chars joined) must NOT lose any of its error list --
that volume is well under the cap.
"""

from infra_brain.etl.base import _ERROR_MESSAGE_MAX_CHARS, _join_errors_truncated


def test_short_error_list_is_untouched():
    errors = ["error one", "error two", "error three"]
    assert _join_errors_truncated(errors) == "error one; error two; error three"


def test_realistic_homelab_error_volume_is_never_truncated():
    """Regression test for the false-DB-limit finding: the documented
    real-world case (homelab_services, 32 skipped-entry errors ~300 chars
    each, ~9800 chars joined) must come through completely intact -- Postgres
    can store far more than this in the unbounded Text column, so nothing
    here should ever be cut."""
    errors = [f"skipped entry {i}: " + ("detail " * 40).strip() for i in range(32)]
    joined = "; ".join(errors)
    assert len(joined) < 10000  # sanity-check the scenario matches the finding

    result = _join_errors_truncated(errors)

    assert result == joined
    assert "...and" not in result


def test_long_error_list_keeps_only_complete_entries():
    """Never cut an entry mid-string -- every included entry is exactly one
    of the original error strings, never a fragment."""
    errors = [f"entry-{i}: " + ("x" * 900) for i in range(50)]
    result = _join_errors_truncated(errors)

    assert len(result) <= _ERROR_MESSAGE_MAX_CHARS
    assert "...and " in result and " more" in result
    # Every entry that appears must appear in full, not truncated mid-string.
    for entry in errors:
        assert entry not in result or result.count(entry) >= 1
    # The included entries are a prefix of the original list, each intact.
    body = result.rsplit("; ...and ", 1)[0]
    included = body.split("; ")
    assert included == errors[: len(included)]


def test_long_error_list_reports_accurate_omitted_count():
    errors = [f"entry-{i}: " + ("x" * 900) for i in range(50)]
    result = _join_errors_truncated(errors)
    body, suffix = result.rsplit("; ...and ", 1)
    omitted = int(suffix.split(" more")[0])
    included_count = len(body.split("; "))
    assert included_count + omitted == len(errors)


def test_single_oversized_entry_has_no_leading_separator():
    """Edge case: even the FIRST error alone exceeds the budget -- the
    omitted-count suffix must not have a stray leading '; ' with nothing
    before it."""
    errors = ["x" * (_ERROR_MESSAGE_MAX_CHARS + 1)]
    result = _join_errors_truncated(errors)
    assert result == "...and 1 more"
    assert not result.startswith("; ")
