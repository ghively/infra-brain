"""mcp_auth: token hashing/generation and key CRUD helpers."""
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_auth

from tests.support.pg import make_engine


@pytest.fixture
def db():
    eng = make_engine()
    with Session(eng) as s:
        yield s


def test_hash_token_matches_sha256():
    assert mcp_auth.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_generate_token_is_unique_and_prefixed():
    a, b = mcp_auth.generate_token(), mcp_auth.generate_token()
    assert a != b
    assert a.startswith("ibmcp_")
    assert len(a) > 30


def test_catalog_partitions_are_disjoint_and_cover_all():
    ro, mut = set(mcp_auth.READONLY_TOOL_NAMES), set(mcp_auth.MUTATION_TOOL_NAMES)
    assert ro.isdisjoint(mut)
    assert set(mcp_auth.ALL_TOOL_NAMES) == ro | mut
    assert "query_resources" in ro
    assert "seed_resource" in mut


def test_catalog_matches_registered_mcp_tools():
    """The auth catalog must list EVERY @mcp.tool in mcp_server.py, exactly.

    ALL_TOOL_NAMES is load-bearing for enforcement, not just for the dashboard
    picker: the auth middleware 403s any tool name absent from a key's
    allowed_tools, and api/schemas.py refuses to mint a key naming a tool
    outside this catalog — so a registered-but-uncatalogued tool is unreachable
    by every key, including the full-access bootstrap key. That is exactly how
    7 tools silently rotted (TRK-231). Introspects the live FastMCP registry
    (``mcp.list_tools()``) rather than grepping decorators, so it cannot be
    fooled by formatting.
    """
    import asyncio  # noqa: PLC0415

    from infra_brain.mcp_server import mcp  # noqa: PLC0415

    # run_middleware=False: we want the raw registry, not the auth-filtered view.
    registered = {t.name for t in asyncio.run(mcp.list_tools(run_middleware=False))}
    catalog = set(mcp_auth.ALL_TOOL_NAMES)

    assert registered - catalog == set(), (
        "@mcp.tool functions missing from mcp_auth's catalog — they will 403 for "
        "every key, even the bootstrap key. Add each to READONLY_TOOL_NAMES or "
        f"MUTATION_TOOL_NAMES: {sorted(registered - catalog)}"
    )
    assert catalog - registered == set(), (
        "catalog names with no @mcp.tool behind them (renamed or removed tool?): "
        f"{sorted(catalog - registered)}"
    )


def test_tool_groups_cover_all_tool_names():
    """TOOL_GROUPS (dashboard picker UI) must partition ALL_TOOL_NAMES exactly:
    every tool in exactly one group, no unknown names, no duplicates across
    groups. Purely a UI-grouping concern, not enforcement — but a drift here
    means a tool silently disappears from (or duplicates in) the create-key
    picker, the same failure class TRK-231 caught for the flat catalog."""
    grouped_names = [n for names in mcp_auth.TOOL_GROUPS.values() for n in names]
    assert len(grouped_names) == len(set(grouped_names)), "a tool appears in >1 group"
    assert set(grouped_names) == set(mcp_auth.ALL_TOOL_NAMES), (
        f"in ALL_TOOL_NAMES but no group: {sorted(set(mcp_auth.ALL_TOOL_NAMES) - set(grouped_names))}; "
        f"in a group but not ALL_TOOL_NAMES: {sorted(set(grouped_names) - set(mcp_auth.ALL_TOOL_NAMES))}"
    )


def test_read_only_governance_tools_are_not_grouped_with_mutation_tools():
    """get_governance_events and verify_governance_chain are pure reads
    (both in READONLY_TOOL_NAMES) but were previously grouped under
    "State backend (...)", a group otherwise made up of mutation tools —
    a group-based key grant in the dashboard could look complete while
    silently omitting these two read tools. Regression guard: both must
    live in a group with no mutation tools.
    """
    for name in ("get_governance_events", "verify_governance_chain"):
        assert name in mcp_auth.READONLY_TOOL_NAMES
        assert name not in mcp_auth.MUTATION_TOOL_NAMES
        group = next(g for g, names in mcp_auth.TOOL_GROUPS.items() if name in names)
        mutating_in_group = set(mcp_auth.TOOL_GROUPS[group]) & set(mcp_auth.MUTATION_TOOL_NAMES)
        assert not mutating_in_group, (
            f"{name} is grouped under {group!r} alongside mutation tools "
            f"{sorted(mutating_in_group)}"
        )


def test_catalog_has_no_duplicate_names():
    assert len(mcp_auth.ALL_TOOL_NAMES) == len(set(mcp_auth.ALL_TOOL_NAMES))


def test_create_key_returns_raw_once_and_stores_only_hash(db):
    row, raw = mcp_auth.create_key(
        db, name="ops", allowed_tools=["query_resources"], created_by="operator"
    )
    db.commit()
    assert raw.startswith("ibmcp_")
    assert row.token_hash == mcp_auth.hash_token(raw)
    # The raw value is never persisted anywhere on the row.
    assert raw not in (row.name, row.token_hash)


def test_lookup_active_key_finds_by_raw_token(db):
    row, raw = mcp_auth.create_key(db, name="ops", allowed_tools=["a", "b"], created_by="g")
    db.commit()
    found = mcp_auth.lookup_active_key(db, raw)
    assert found is not None
    key_id, tools = found
    assert key_id == row.id
    assert tools == ["a", "b"]


def test_lookup_active_key_returns_none_for_unknown_and_revoked(db):
    row, raw = mcp_auth.create_key(db, name="ops", allowed_tools=["a"], created_by="g")
    db.commit()
    assert mcp_auth.lookup_active_key(db, "ibmcp_nope") is None
    assert mcp_auth.revoke_key(db, row.id) is True
    db.commit()
    assert mcp_auth.lookup_active_key(db, raw) is None


def test_touch_last_used_sets_timestamp_and_never_raises(db):
    row, raw = mcp_auth.create_key(db, name="ops", allowed_tools=[], created_by="g")
    db.commit()
    mcp_auth.touch_last_used(db, row.id)
    db.commit()
    db.refresh(row)
    assert row.last_used_at is not None
    # A bogus id is a silent no-op, never an exception.
    mcp_auth.touch_last_used(db, uuid.uuid4())


def test_revoke_key_returns_false_when_already_revoked(db):
    row, raw = mcp_auth.create_key(db, name="ops", allowed_tools=[], created_by="g")
    db.commit()
    assert mcp_auth.revoke_key(db, row.id) is True
    db.commit()
    assert mcp_auth.revoke_key(db, row.id) is False


def test_create_bootstrap_key_is_full_access(db):
    row, raw = mcp_auth.create_bootstrap_key(db)
    db.commit()
    assert row.name == "bootstrap"
    assert set(row.allowed_tools) == set(mcp_auth.ALL_TOOL_NAMES)


# ── Optional expiry (TRK-160) ────────────────────────────────────────────────
# A leaked key used to stay valid forever until someone manually revoked it.
# ``expires_at`` is enforced at the SAME choke point as ``revoked_at`` —
# lookup_active_key — so an expired key is indistinguishable from a revoked one
# to the caller (401 / DENY_KEY_INVALID, already literally named
# "key_invalid_or_expired" in mcp_server.py) and lands in the same denial-audit
# path. NULL means "never expires", so every pre-existing key is unaffected.


def test_lookup_active_key_returns_none_for_expired_key(db):
    row, raw = mcp_auth.create_key(
        db, name="ops", allowed_tools=["a"], created_by="g", expires_in_days=1
    )
    db.commit()
    # Unexpired: still usable.
    assert mcp_auth.lookup_active_key(db, raw) is not None
    # Backdate past the expiry and it must fail auth exactly like a revoked key.
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert mcp_auth.lookup_active_key(db, raw) is None


def test_lookup_active_key_null_expiry_never_expires(db):
    """The default, and the whole reason this is a non-breaking change: a key
    minted without an expiry (every key that existed before TRK-160) stays
    valid no matter how far the clock moves."""
    row, raw = mcp_auth.create_key(db, name="ops", allowed_tools=["a"], created_by="g")
    db.commit()
    assert row.expires_at is None
    assert mcp_auth.lookup_active_key(db, raw) is not None
    assert mcp_auth.is_expired(None, now=datetime(2999, 1, 1, tzinfo=UTC)) is False


def test_lookup_active_key_name_also_honors_expiry(db):
    """Identity attribution must not disagree with the auth decision — an
    expired key cannot be used to attribute a write to itself."""
    row, raw = mcp_auth.create_key(db, name="attributor", allowed_tools=["a"], created_by="g")
    db.commit()
    assert mcp_auth.lookup_active_key_name(db, raw) == "attributor"
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    db.commit()
    assert mcp_auth.lookup_active_key_name(db, raw) is None


def test_is_expired_boundary_is_inclusive_fail_closed():
    """Exactly-now is EXPIRED. In an auth check the tie-break must deny."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert mcp_auth.is_expired(now, now=now) is True
    assert mcp_auth.is_expired(now + timedelta(microseconds=1), now=now) is False
    assert mcp_auth.is_expired(now - timedelta(microseconds=1), now=now) is True


def test_naive_expires_at_is_treated_as_utc_not_a_typeerror():
    """The security hole this guards: SQLite hands back NAIVE datetimes for a
    DateTime(timezone=True) column. A bare `naive <= aware` raises TypeError
    inside the auth path — which is at best a 500 and, under any caller that
    swallows exceptions, a fail-OPEN. Assert it compares, and compares as UTC.
    """
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    naive_past = datetime(2026, 1, 1, 11, 0, 0)  # noqa: DTZ001 - deliberately naive
    naive_future = datetime(2026, 1, 1, 13, 0, 0)  # noqa: DTZ001 - deliberately naive
    assert mcp_auth.is_expired(naive_past, now=now) is True
    assert mcp_auth.is_expired(naive_future, now=now) is False
    # Interpreted as UTC, not as local time: an hour before "now" in UTC is
    # expired regardless of the machine's zone.
    assert mcp_auth._as_utc(naive_past).tzinfo is UTC


def test_expiry_from_days_rejects_out_of_range_and_nonpositive():
    assert mcp_auth.expiry_from_days(None) is None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert mcp_auth.expiry_from_days(30, now=now) == now + timedelta(days=30)
    assert mcp_auth.expiry_from_days(mcp_auth.MAX_EXPIRES_DAYS, now=now) is not None
    for bad in (0, -1, mcp_auth.MAX_EXPIRES_DAYS + 1):
        with pytest.raises(ValueError):
            mcp_auth.expiry_from_days(bad)


def test_create_key_with_expiry_sets_absolute_instant(db):
    before = mcp_auth._now_utc()
    row, _raw = mcp_auth.create_key(
        db, name="ops", allowed_tools=["a"], created_by="g", expires_in_days=7
    )
    db.commit()
    assert row.expires_at is not None
    assert before + timedelta(days=6) < mcp_auth._as_utc(row.expires_at)
    assert mcp_auth._as_utc(row.expires_at) < before + timedelta(days=8)


def test_create_bootstrap_key_expiry_defaults_to_never(db):
    row, _raw = mcp_auth.create_bootstrap_key(db)
    db.commit()
    assert row.expires_at is None
    row2, _raw2 = mcp_auth.create_bootstrap_key(db, expires_in_days=90)
    db.commit()
    assert row2.expires_at is not None


def test_parse_bootstrap_argv_flag_forms_and_errors():
    """The CLI flag: both spellings, absent = never expires, bad = ValueError."""
    assert mcp_auth.parse_bootstrap_argv(["--bootstrap"]) is None
    assert mcp_auth.parse_bootstrap_argv(["--bootstrap", "--expires-days", "30"]) == 30
    assert mcp_auth.parse_bootstrap_argv(["--bootstrap", "--expires-days=30"]) == 30
    for bad in (
        ["--bootstrap", "--expires-days"],  # missing value
        ["--bootstrap", "--expires-days", "abc"],  # not an integer
        ["--bootstrap", "--expires-days", "0"],  # non-positive
        ["--bootstrap", "--expires-days", "99999"],  # over the ceiling
    ):
        with pytest.raises(ValueError):
            mcp_auth.parse_bootstrap_argv(bad)
