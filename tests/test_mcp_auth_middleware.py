"""Scoped-key MCP auth middleware: 401 no/unknown key, 403 out-of-scope tool,
200 in-scope tool, dev bypass, and best-effort last_used_at."""

import asyncio
import json
import socket
import threading
from contextlib import contextmanager

import pytest
import uvicorn
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from infra_brain import mcp_auth, mcp_server
from infra_brain.db.models import Base


@pytest.fixture(autouse=True)
def _reset_auth_denial_rate_limiter():
    """GitLab #171's dedup state is process-global (module-level dict), so
    tests in this file must not bleed into each other via it -- otherwise a
    denial in one test could suppress the audit row a LATER test expects,
    purely because both happened to use the same token+reason within the
    same 60s window during a fast test run."""
    mcp_server._auth_denial_last_write.clear()
    mcp_server._auth_denial_window_start = 0.0
    mcp_server._auth_denial_window_count = 0
    yield
    mcp_server._auth_denial_last_write.clear()
    mcp_server._auth_denial_window_start = 0.0
    mcp_server._auth_denial_window_count = 0


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """File-backed DB with one active key scoped to query_resources, the MCP
    auth middleware patched to use it, and a JSON-RPC echo route behind it.

    TRK-367: this used to be an in-memory sqlite engine on ``poolclass=
    StaticPool``, which hands every ``Session`` checkout the SAME physical
    DBAPI connection. That's fine for the single-threaded TestClient tests
    in this file, but ``test_full_protocol_round_trip_through_real_app_and_
    middleware`` below drives the real uvicorn transport, where
    ``_authorize`` genuinely runs concurrently across more than one
    ``asyncio.to_thread`` worker -- two ``Session``s committing on one
    shared connection race each other's transaction state (reproduced
    locally as ``sqlite3.OperationalError: cannot commit - no transaction
    is active``, matching CI pipeline 1219 verbatim). Production Postgres
    never shares a connection across concurrent sessions, so it was never
    exposed to this.

    A ``tmp_path``-backed sqlite file gives every ``Session`` checkout its
    own connection (the default pool for a file-based sqlite engine is
    ``QueuePool``, not the single-shared-connection ``StaticPool``) --
    matching production's connection-per-session shape while staying
    sqlite (no real DB needed). ``tmp_path`` is pytest's own per-test
    tempdir, torn down by pytest's own tmp-dir retention/cleanup policy, so
    no manual file cleanup is needed here; ``eng.dispose()`` still closes
    the pool's connections so nothing is left open past the test.

    GATE-4 INTEGRATION NOTE (rev13): this file is deliberately NOT in
    ``tests/support/agent_orm_check_paths.txt``. ``make_engine()`` is a
    StaticPool chooser on BOTH dialects (its cross-thread visibility
    semantics are what the other 96 gated modules need), which is exactly
    the shared-connection shape this file's real-transport concurrency test
    exists to avoid. The expiry/auth ORM surface still meets real
    PostgreSQL through ``test_mcp_auth_helpers.py`` / ``test_mcp_api_key_
    model.py`` / ``test_mcp_keys_api.py``, which ARE gated.
    """
    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    db_path = tmp_path / "mcp_auth_wired.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        # This is a throwaway per-test file that never needs to survive a
        # crash, so trade the durability guarantees (fsync-per-commit) for
        # test speed: WAL lets concurrent readers/writers avoid blocking
        # each other (needed now that real concurrency is the whole point
        # of this fixture, see the class docstring above), and
        # synchronous=OFF skips the fsync WAL would otherwise still do on
        # checkpoint -- Base.metadata.create_all() alone was ~3-4s/test
        # without this, from every DDL statement's default fsync.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    Base.metadata.create_all(eng)
    with Session(eng) as s:
        _, raw = mcp_auth.create_key(
            s, name="ops", allowed_tools=["query_resources"], created_by="t"
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr(mcp_server, "get_session", _get_session)

    async def _rpc(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/mcp", _rpc, methods=["POST"])],
        middleware=[Middleware(mcp_server._ApiKeyAuthMiddleware)],
    )
    yield TestClient(app), raw, eng
    eng.dispose()


def _call(name):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}})


def test_no_bearer_is_401(wired):
    client, raw, _ = wired
    resp = client.post("/mcp", content=_call("query_resources"))
    assert resp.status_code == 401


def test_unknown_key_is_401(wired):
    client, raw, _ = wired
    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": "Bearer ibmcp_bogus"}
    )
    assert resp.status_code == 401


def test_in_scope_tool_call_is_200(wired):
    client, raw, _ = wired
    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_out_of_scope_tool_call_is_403(wired):
    client, raw, _ = wired
    resp = client.post(
        "/mcp", content=_call("seed_resource"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 403


def test_non_toolcall_method_only_needs_valid_key(wired):
    client, raw, _ = wired
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    resp = client.post("/mcp", content=body, headers={"authorization": f"Bearer {raw}"})
    assert resp.status_code == 200


def test_dev_mode_bypasses_auth(wired, monkeypatch):
    client, raw, _ = wired
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    resp = client.post("/mcp", content=_call("seed_resource"))
    assert resp.status_code == 200


def test_last_used_at_updated_on_success(wired):
    client, raw, eng = wired
    client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    from infra_brain.db.models import McpApiKey

    with Session(eng) as s:
        row = s.query(McpApiKey).one()
        assert row.last_used_at is not None


def test_revoked_key_is_401(wired):
    """A revoked key must be denied through the middleware, not just at the
    lookup_active_key() helper level (lc-safety-reviewer finding: this was
    only proven at the helper level, not end-to-end through the middleware)."""
    client, raw, eng = wired
    from infra_brain.db.models import McpApiKey

    with Session(eng) as s:
        key_row = s.query(McpApiKey).one()
        mcp_auth.revoke_key(s, key_row.id)
        s.commit()

    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 401


def test_expired_key_is_401_end_to_end(wired):
    """TRK-160: an EXPIRED key must be denied through the middleware, exactly
    like a revoked one — same 401, same DENY_KEY_INVALID reason code, same
    JSON body. Proving this end-to-end (not just at lookup_active_key) is the
    same gap test_revoked_key_is_401 above exists to close."""
    from datetime import UTC, datetime, timedelta

    from infra_brain.db.models import McpApiKey

    client, raw, eng = wired
    # Sanity: the key works before it expires, so the assertion below is about
    # expiry and not about some unrelated breakage in the fixture.
    ok = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert ok.status_code == 200

    with Session(eng) as s:
        key_row = s.query(McpApiKey).one()
        key_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()

    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == mcp_server.DENY_KEY_INVALID


def test_expired_key_deny_is_audited_like_any_other_denial(wired):
    """The failure must be visible in the SAME audit path revoked/unknown-key
    failures use — an expiry that denies silently is an expiry no operator can
    diagnose."""
    from datetime import UTC, datetime, timedelta

    from infra_brain.db.models import McpApiKey

    client, raw, eng = wired
    with Session(eng) as s:
        key_row = s.query(McpApiKey).one()
        key_row.expires_at = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 401
    _assert_single_deny_row(
        eng, reason=mcp_server.DENY_KEY_INVALID, tool="query_resources", token=raw
    )


def test_key_expiring_in_the_future_still_authorizes(wired):
    """The other half of the contract: setting an expiry must not break a key
    that has not reached it."""
    from datetime import UTC, datetime, timedelta

    from infra_brain.db.models import McpApiKey

    client, raw, eng = wired
    with Session(eng) as s:
        key_row = s.query(McpApiKey).one()
        key_row.expires_at = datetime.now(UTC) + timedelta(days=30)
        s.commit()

    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 200


def test_db_error_during_authorize_denies_not_allows(wired, monkeypatch):
    """A DB failure during the auth lookup must never fail open (lc-safety-
    reviewer finding: the single most important property given loopback
    protection is removed — this is the regression test that would catch a
    future refactor wrapping _authorize in a broad try/except that
    accidentally allows on error)."""
    client, raw, _ = wired

    def _broken_get_session():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(mcp_server, "get_session", _broken_get_session)

    # TestClient re-raises server exceptions by default (raise_server_exceptions=True) —
    # in production uvicorn would turn this into a 500, but either way the tool
    # must never dispatch and no 200/"ok" response must ever be produced.
    with pytest.raises(RuntimeError, match="db unreachable"):
        client.post(
            "/mcp",
            content=_call("query_resources"),
            headers={"authorization": f"Bearer {raw}"},
        )


def test_metrics_path_is_exempt_from_auth(wired):
    """/metrics must be reachable with no Authorization header — it's the
    same no-auth trust tier as main.py's /healthz (lc-safety-reviewer
    finding: the middleware wraps the whole app, so without this exemption
    Prometheus scraping silently 401s in every non-dev environment)."""
    client, _, _ = wired
    from starlette.responses import PlainTextResponse as _PTR
    from starlette.routing import Route as _Route

    client.app.routes.append(_Route("/metrics", lambda r: _PTR("mcp_tool_calls_total 0\n")))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "mcp_tool_calls_total" in resp.text


def _batch(*names):
    """A JSON-RPC batch array: one tools/call entry per name."""
    return json.dumps(
        [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n}}
            for i, n in enumerate(names)
        ]
    )


def test_single_toolcall_still_scope_checks(wired):
    """Regression: the single-object tools/call path must keep enforcing scope
    after the batch-array rewrite (in-scope 200, out-of-scope 403)."""
    client, raw, _ = wired
    ok = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert ok.status_code == 200
    denied = client.post(
        "/mcp", content=_call("seed_resource"), headers={"authorization": f"Bearer {raw}"}
    )
    assert denied.status_code == 403


def test_batch_with_out_of_scope_tool_is_403(wired):
    """A JSON-RPC batch array must be scope-checked per entry — a batch that
    includes a tool outside the key's scope is denied even if another entry is
    in-scope. This is the fail-open bug the rewrite closes."""
    client, raw, _ = wired
    resp = client.post(
        "/mcp",
        content=_batch("query_resources", "seed_resource"),
        headers={"authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403


def test_batch_all_in_scope_is_allowed(wired):
    """A batch whose every entry is in-scope passes (auth + scope both satisfied)."""
    client, raw, _ = wired
    resp = client.post(
        "/mcp",
        content=_batch("query_resources", "query_resources"),
        headers={"authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200


def test_ambiguous_batch_entry_is_denied(wired):
    """A batch containing an un-classifiable tools/call entry (a non-dict item,
    or a tools/call whose params/name is malformed) must fail closed → 403 for a
    scope-restricted key, never silently skip the scope check."""
    client, raw, _ = wired
    # non-dict batch item
    r1 = client.post(
        "/mcp", content=json.dumps([123]), headers={"authorization": f"Bearer {raw}"}
    )
    assert r1.status_code == 403
    # tools/call entry whose name is not a string
    r2 = client.post(
        "/mcp",
        content=json.dumps(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 123}}]
        ),
        headers={"authorization": f"Bearer {raw}"},
    )
    assert r2.status_code == 403


def test_batch_non_toolcall_entries_are_skipped_not_denied(wired):
    """Batch entries whose method is NOT tools/call are skipped for scope (not
    treated as ambiguous) — a batch of only non-tools/call methods needs auth
    only and passes."""
    client, raw, _ = wired
    resp = client.post(
        "/mcp",
        content=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]),
        headers={"authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200


def test_tools_list_and_empty_body_are_auth_only(wired):
    """tools/list, initialize, and empty bodies must still pass with auth-only
    (no scope check) — this must NOT regress under the fail-closed rewrite."""
    client, raw, _ = wired
    hdr = {"authorization": f"Bearer {raw}"}
    for body in (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "",
    ):
        resp = client.post("/mcp", content=body, headers=hdr)
        assert resp.status_code == 200, body


def test_requested_tool_names_contract():
    """Direct unit coverage of the classifier's contract: [] = no scope check,
    list = check all, None = fail closed."""
    f = mcp_server._requested_tool_names
    assert f(_call("query_resources").encode()) == ["query_resources"]
    assert f(_batch("a", "b").encode()) == ["a", "b"]
    assert f(json.dumps({"jsonrpc": "2.0", "method": "tools/list"}).encode()) == []
    assert f(b"") == []
    assert f(b"not json") is None
    assert f(json.dumps({"method": "tools/call", "params": {}}).encode()) is None
    assert f(json.dumps([123]).encode()) is None
    # non-tools/call batch entries are skipped, yielding []
    assert f(json.dumps([{"method": "tools/list"}]).encode()) == []


def test_websocket_scope_is_refused_not_forwarded():
    """A non-http/non-lifespan ASGI scope (e.g. websocket) must be refused: the
    wrapped inner app is NEVER called and nothing is sent. This closes the
    dormant trap where a future websocket route would bypass auth entirely."""
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1

    mw = mcp_server._ApiKeyAuthMiddleware(inner)

    async def _run():
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "websocket.disconnect"}

        await mw({"type": "websocket"}, receive, send)
        return sent

    sent = asyncio.run(_run())
    assert called["n"] == 0
    assert sent == []


def test_lifespan_scope_passes_through_unchanged():
    """lifespan must always pass through to the inner app (startup/shutdown)."""
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1

    mw = mcp_server._ApiKeyAuthMiddleware(inner)

    async def _run():
        async def send(m):
            pass

        async def receive():
            return {"type": "lifespan.startup"}

        await mw({"type": "lifespan"}, receive, send)

    asyncio.run(_run())
    assert called["n"] == 1


# ── Deny-path audit trail (GitLab #167) ──────────────────────────────────────
# Every deny at this layer must leave exactly one agent_action_log row with the
# right reason code, hashed key, and NO raw token anywhere in the row.


def _deny_rows(eng):
    from infra_brain.db.models import AgentActionLog

    with Session(eng) as s:
        return s.query(AgentActionLog).all()


def _assert_single_deny_row(eng, *, reason, tool, token):
    rows = _deny_rows(eng)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    row = rows[0]
    assert row.agent == "mcp"
    assert row.domain == "mcp"
    assert row.verdict == "deny"
    assert row.status == "denied"
    assert row.error == reason
    assert row.tool == tool
    assert row.caller_key_hash == mcp_auth.hash_token(token)
    # The raw token must appear NOWHERE in the persisted row.
    serialized = " ".join(str(getattr(row, c.key)) for c in row.__table__.columns)
    assert token not in serialized
    return row


def test_unknown_key_deny_is_audited(wired):
    """(a) unknown/expired token → 401 unchanged + one deny row."""
    client, _, eng = wired
    bogus = "ibmcp_bogus_token_value"
    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {bogus}"}
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == mcp_server.DENY_KEY_INVALID
    _assert_single_deny_row(
        eng, reason=mcp_server.DENY_KEY_INVALID, tool="query_resources", token=bogus
    )


def test_out_of_scope_deny_is_audited(wired):
    """(b) valid key calling a tool outside its scope → 403 unchanged + one deny row."""
    client, raw, eng = wired
    resp = client.post(
        "/mcp", content=_call("seed_resource"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == mcp_server.DENY_KEY_LACKS_SCOPE
    _assert_single_deny_row(
        eng, reason=mcp_server.DENY_KEY_LACKS_SCOPE, tool="seed_resource", token=raw
    )


def test_unparseable_body_deny_is_audited(wired):
    """(c) malformed/unparseable request body → 403 unchanged + one deny row.

    tool is "unknown" because no tool name could be extracted at all."""
    client, raw, eng = wired
    resp = client.post(
        "/mcp", content="{not json at all", headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == mcp_server.DENY_UNPARSEABLE
    _assert_single_deny_row(eng, reason=mcp_server.DENY_UNPARSEABLE, tool="unknown", token=raw)


def test_deny_row_tool_redacts_pan_straddling_the_128char_truncation(wired):
    """Regression test: _record_auth_denial must redact_pans() BEFORE
    truncating the requested-tool-name string to the ``tool`` column's
    128-char limit, not after — the opposite order lets a PAN-shaped digit
    run that straddles the cut survive unredacted in ``agent_action_log.tool``
    (which get_agent_activity later serves verbatim).

    Built so the cut lands INSIDE a real, Luhn-valid, 16-digit Visa test PAN
    ("4111111111111111"): a 113-char ``-`` prefix (dashes, not letters, so the
    DLP regex's ``\\b`` word boundary actually falls right before the digit
    run -- alphanumeric padding would suppress the match entirely) pushes the
    PAN to start at index 113, so slicing the combined (unredacted) string to
    [:128] keeps only its first 15 digits. That 15-digit fragment still
    matches the DLP regex's 13-19 digit-count shape but is neither a
    Luhn-valid number nor a valid Visa length (13/16/19) on its own, so
    ``is_probable_pan`` rejects it -- exactly the "no longer matches the
    digit-count/word-boundary/Luhn requirements" gap the finding describes.
    Redacting the FULL PAN first (correct order) replaces it with the
    ``REDACTED`` marker before the 128-char cut ever happens, so no raw
    digits can survive either way.
    """
    client, raw, eng = wired
    pan = "4111111111111111"  # Luhn-valid Visa test number (16 digits)
    prefix = "-" * 113
    malicious_tool_name = prefix + pan
    truncated_raw_fragment = (prefix + pan)[113:128]  # the 15 raw digits a buggy
    # truncate-then-redact order would leave exposed
    assert truncated_raw_fragment == pan[:15]

    resp = client.post(
        "/mcp",
        content=_call(malicious_tool_name),
        headers={"authorization": "Bearer ibmcp_bogus_token_value"},
    )
    assert resp.status_code == 401

    rows = _deny_rows(eng)
    assert len(rows) == 1
    row = rows[0]
    assert len(row.tool) <= 128
    # The raw (unredacted) digit fragment must not appear anywhere in the
    # persisted tool column.
    assert truncated_raw_fragment not in row.tool
    # And the redaction marker must actually be present, proving the PAN was
    # caught (not just silently truncated away by coincidence).
    assert "REDACTED" in row.tool


def test_batch_deny_records_every_requested_tool(wired):
    """Scope is all-or-nothing across a JSON-RPC batch, so the deny row names
    every requested tool — not an arbitrary one that may not be the offender."""
    client, raw, eng = wired
    resp = client.post(
        "/mcp",
        content=_batch("query_resources", "seed_resource"),
        headers={"authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403
    row = _assert_single_deny_row(
        eng,
        reason=mcp_server.DENY_KEY_LACKS_SCOPE,
        tool="query_resources,seed_resource",
        token=raw,
    )
    assert json.loads(row.args_summary)["requested_tools"] == [
        "query_resources",
        "seed_resource",
    ]


def test_repeated_denial_from_same_token_is_rate_limited(wired):
    """GitLab #171: a burst of denials from the SAME bad token must not write
    one agent_action_log row per request -- only the first in the window."""
    client, _, eng = wired
    bogus = "ibmcp_repeated_bogus_token"
    for _ in range(5):
        resp = client.post(
            "/mcp",
            content=_call("query_resources"),
            headers={"authorization": f"Bearer {bogus}"},
        )
        assert resp.status_code == 401  # the deny decision itself is never suppressed

    # Only the FIRST denial in the window actually wrote a row.
    _assert_single_deny_row(
        eng, reason=mcp_server.DENY_KEY_INVALID, tool="query_resources", token=bogus
    )


def test_denials_from_different_tokens_are_each_audited_up_to_the_global_cap(wired):
    """The per-(key,reason) dedup alone doesn't help against an attacker
    rotating a FRESH bogus token every request -- confirm the global window
    cap still bounds writes across distinct callers, not just repeats."""
    client, _, eng = wired
    original_max = mcp_server._AUTH_DENIAL_WINDOW_MAX_WRITES
    mcp_server._AUTH_DENIAL_WINDOW_MAX_WRITES = 3
    try:
        for i in range(6):
            resp = client.post(
                "/mcp",
                content=_call("query_resources"),
                headers={"authorization": f"Bearer distinct-bogus-token-{i}"},
            )
            assert resp.status_code == 401
        assert len(_deny_rows(eng)) == 3
    finally:
        mcp_server._AUTH_DENIAL_WINDOW_MAX_WRITES = original_max


def test_allowed_call_writes_no_deny_row(wired):
    """A permitted call must NOT write a deny row here (McpAuditMiddleware owns
    the allow-path row, and it is not in this test's middleware stack)."""
    client, raw, eng = wired
    resp = client.post(
        "/mcp", content=_call("query_resources"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 200
    assert _deny_rows(eng) == []


def test_audit_write_failure_still_denies(wired, monkeypatch):
    """A DB blip while writing the deny row must not turn the 403 into a 500."""
    client, raw, _ = wired
    real_get_session = mcp_server.get_session
    calls = {"n": 0}

    def _flaky_get_session(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:  # first call = the auth lookup, second = the audit write
            raise RuntimeError("audit table unavailable")
        return real_get_session(*a, **kw)

    monkeypatch.setattr(mcp_server, "get_session", _flaky_get_session)
    resp = client.post(
        "/mcp", content=_call("seed_resource"), headers={"authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 403
    assert calls["n"] == 2, "the audit write must actually have been attempted"


def test_no_bearer_401_is_json_shaped(wired):
    """The no-token 401 stays a 401 and is JSON-shaped (no audit row by design —
    there is no key identity to attribute and it precedes body buffering)."""
    client, _, eng = wired
    resp = client.post("/mcp", content=_call("query_resources"))
    assert resp.status_code == 401
    assert resp.json()["error"]
    assert _deny_rows(eng) == []


def test_write_scope_table_is_derived_from_mutation_tool_names():
    """The write-scope doc table must be generated from MUTATION_TOOL_NAMES so
    it cannot drift out of sync with enforcement."""
    table = mcp_auth.write_scope_tool_table()
    for name in mcp_auth.MUTATION_TOOL_NAMES:
        assert f"`{name}`" in table
    for name in mcp_auth.READONLY_TOOL_NAMES:
        assert f"`{name}`" not in table


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server_thread(app, port: int, ready: threading.Event, stop: threading.Event) -> None:
    async def _serve():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # not the main thread
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                # Surface the real startup failure (e.g. a lost port-bind
                # race) instead of spinning forever and later failing the
                # test with a misleading "did not start in time".
                task.result()
                return
            await asyncio.sleep(0.01)
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.should_exit = True
        await task

    asyncio.run(_serve())


async def _drive_real_client(port: int, token: str) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {token}"}
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        assert len(tools) > 50  # the real production tool registry, not a stub
        result = await client.call_tool("query_resources", {})
        assert result.data == []  # empty in-memory DB, but the call must complete


def test_full_protocol_round_trip_through_real_app_and_middleware(wired):
    """Regression test for a real incident: the middleware's `_replay()` used
    to fabricate an immediate `http.disconnect` on every receive() call after
    the first, instead of delegating to the real receive() channel. FastMCP's
    streamable-http session manager calls receive() again later in a
    long-lived connection (e.g. to detect a genuine client disconnect); the
    fabricated disconnect made it think the client hung up right after the
    request, so it aborted before writing a response — silently breaking
    EVERY tools/call and the SSE reconnect stream for every real client, while
    the mocked-downstream tests above (a fake PlainTextResponse route hit
    once via the synchronous TestClient) stayed green throughout, because
    they never exercise the real multi-receive() session lifecycle. This test
    runs the actual `mcp.http_app()` + `_ApiKeyAuthMiddleware` behind a real
    uvicorn server and drives it with the real fastmcp streamable-http
    client — the only way to catch this class of bug.
    """
    _, raw, _ = wired  # reuses the `wired` fixture's in-memory DB + key, not its fake app

    app = mcp_server.mcp.http_app(middleware=[Middleware(mcp_server._ApiKeyAuthMiddleware)])
    port = _free_port()
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=_run_server_thread, args=(app, port, ready, stop), daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=10), "server did not start in time"
        # A tight timeout turns a regression back into this exact bug class
        # (silent hang) into a clear, fast test FAILURE instead of a CI hang.
        asyncio.run(asyncio.wait_for(_drive_real_client(port, raw), timeout=15))
    finally:
        stop.set()
        thread.join(timeout=10)


def test_concurrent_authorize_does_not_race_the_shared_connection(wired):
    """TRK-367 regression: CI pipeline 1219 (2026-08-13) hit a server-side
    ``sqlite3.OperationalError: cannot commit - no transaction is active``
    from ``_authorize``'s ``s.commit()`` in
    ``test_full_protocol_round_trip_through_real_app_and_middleware``, which
    drives the real uvicorn transport (so ``_authorize`` runs concurrently in
    more than one ``asyncio.to_thread`` worker at once).

    ``_authorize`` opens a fresh ``Session`` per call via the fixture-patched
    ``mcp_server.get_session`` -- fine on Postgres (connection-per-session)
    but NOT fine if that engine's pool hands out the SAME physical
    connection to concurrent sessions, which is exactly what a StaticPool
    in-memory sqlite engine does. Two sessions committing on one shared
    DBAPI connection race each other's transaction state.

    This test hammers ``_authorize`` from several threads at once across
    many trials -- reproduced the exact CI error locally (verbatim message,
    plus related shared-connection corruption symptoms: ``SystemError``,
    ``StaleDataError``) against the old StaticPool-backed ``wired`` fixture
    at 6 threads x 300 trials, consistently landing 15-25 failing trials out
    of 300 across repeated runs (see the git history of this test for that
    RED evidence -- it predates the fixture fix). ``n_trials`` is tuned down
    to 60 here now that the fixture is fixed: the fix is structural (every
    ``Session`` gets its own connection), not statistical, so this must stay
    at exactly zero errors across every trial regardless of trial count --
    not just "usually zero" -- and a smaller trial count keeps this test's
    runtime reasonable while still re-proving the property on every run.
    """
    _, raw, _ = wired

    n_threads = 6
    n_trials = 60
    errors: list[str] = []
    lock = threading.Lock()

    def worker(barrier: threading.Barrier) -> None:
        barrier.wait()
        try:
            ok, status, reason = mcp_server._authorize(raw, ["query_resources"])
            if not ok:
                with lock:
                    errors.append(f"unexpected deny: status={status} reason={reason}")
        except Exception as e:  # noqa: BLE001 -- the race raises OperationalError
            with lock:
                errors.append(f"{type(e).__name__}: {e}")

    for _ in range(n_trials):
        barrier = threading.Barrier(n_threads)
        threads = [threading.Thread(target=worker, args=(barrier,)) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == [], (
        f"{len(errors)} of {n_trials} trials raced the shared connection: {errors[:5]}"
    )
