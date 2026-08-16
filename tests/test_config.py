import pytest

from infra_brain.config import Settings, get_settings


def test_defaults_present():
    s = get_settings()
    assert s.dlp_fail_closed is True
    assert s.scan_readonly_enforce is True


def test_integration_confidence_gate_field_present():
    """TRK-182 follow-up: the 0.70 confidence gate (fleet.py applied_instincts
    count + Intprops.tsx) must be a real Settings field, not a hardcoded
    literal duplicated in multiple places."""
    s = Settings()
    assert hasattr(s, "integration_confidence_gate")
    assert s.integration_confidence_gate == 0.70


def test_integration_confidence_gate_override_via_env(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CONFIDENCE_GATE", "0.85")
    s = Settings()
    assert s.integration_confidence_gate == 0.85


def test_k8s_cluster_name_field_present():
    """Reverse-TRK-051 fix: agents/k8s.py:170 reads this field via getattr — it
    must actually exist on Settings so the fallback is live, not always-None."""
    s = Settings()
    assert hasattr(s, "k8s_cluster_name")
    assert s.k8s_cluster_name == ""


def test_dead_config_fields_removed():
    """#83: these fields had zero consumers anywhere in src/infra_brain/ and
    must be fully removed, not just left unused."""
    s = Settings()
    for dead in (
        "api_max_retries",
        "infra_hsa_zone",
        "infra_ops_root",
        "infra_ops_env_max_age_days",
        "n8n_url",
        "n8n_api_key",
        "n8n_webhook_secret",
        "netdiscovery_port_scan_window",
        "netdiscovery_scan_delay",
        "ollama_base_url",
    ):
        assert not hasattr(s, dead), f"{dead} should have been removed (#83)"


def test_postgres_url_required(monkeypatch):
    """An empty POSTGRES_URL must raise — no silent localhost fallback.

    This is the guard that turns a broken env_file/DATABASE_URL wiring into an
    immediate failure instead of the migrate service quietly upgrading an empty
    localhost DB (the divergent-DB failure behind the resource_id 500).
    """
    import pydantic

    monkeypatch.setenv("POSTGRES_URL", "")
    with pytest.raises(pydantic.ValidationError):
        Settings()
    # A real DSN constructs fine.
    monkeypatch.setenv("POSTGRES_URL", "postgresql://infra:infra@db:5432/infra_brain")
    assert Settings().postgres_url.startswith("postgresql")


def test_override_via_env(monkeypatch):
    """get_settings() must pick up an env override after its cache is cleared.

    NOTE: this used to do `importlib.reload(infra_brain.config)` to force a
    re-read. That reload rebinds `infra_brain.config.get_settings` to a BRAND
    NEW function object with a fresh lru_cache — but every other module that
    did `from infra_brain.config import get_settings` at import time (e.g.
    dashboard_auth.py) keeps its own reference to the OLD function forever,
    permanently disconnected from any cache_clear() called on the new one for
    the rest of the test session. That silently froze dashboard_auth's
    dev-mode check for every test running after this one. get_settings() is
    an lru_cache-decorated function — clearing its cache achieves the same
    "pick up the new env value" behavior without reloading the module or
    breaking other modules' bindings.
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("AWS_ENABLED", "1")
    get_settings.cache_clear()
    assert get_settings().aws_enabled is True
    get_settings.cache_clear()  # restore for subsequent tests


# Task 5: Config cleanup tests
def test_semaphore_config_removed():
    s = Settings()
    assert not hasattr(s, "semaphore_url"), "Semaphore was decommissioned; config must be removed"
    assert not hasattr(s, "semaphore_api_key"), (
        "Semaphore was decommissioned; config must be removed"
    )
    assert not hasattr(s, "semaphore_ssl_verify"), (
        "Semaphore was decommissioned; config must be removed"
    )


def test_vsphere_config_present(monkeypatch):
    monkeypatch.delenv("VSPHERE_HOST", raising=False)
    monkeypatch.delenv("VSPHERE_USER", raising=False)
    monkeypatch.delenv("VSPHERE_PASSWORD", raising=False)
    monkeypatch.delenv("VSPHERE_SSL_VERIFY", raising=False)
    s = Settings()
    assert hasattr(s, "vsphere_host")
    assert hasattr(s, "vsphere_user")
    assert hasattr(s, "vsphere_password")
    assert hasattr(s, "vsphere_ssl_verify")
    assert s.vsphere_host == ""
    assert s.vsphere_user == ""
    assert s.vsphere_password == ""
    assert s.vsphere_ssl_verify is True


def test_pagination_config_present():
    s = Settings()
    assert s.api_page_size == 100
    assert s.api_timeout_seconds == 30


class TestCookieSecure:
    """cookie_secure defaults True; empty/falsy env vars coerce to False.

    The same empty-string-safe validator covers both infra_brain_dev and
    cookie_secure so ``COOKIE_SECURE=`` (docker-compose ${VAR:-} with unset host
    var) does not crash pydantic with a ValidationError.
    """

    def test_defaults_true(self, monkeypatch):
        monkeypatch.delenv("COOKIE_SECURE", raising=False)
        assert Settings().cookie_secure is True

    def test_empty_string_coerces_to_false(self, monkeypatch):
        """docker-compose ${COOKIE_SECURE:-true} prevents this, but coercion is defense-in-depth."""
        monkeypatch.setenv("COOKIE_SECURE", "")
        assert Settings().cookie_secure is False

    def test_false_string_is_false(self, monkeypatch):
        monkeypatch.setenv("COOKIE_SECURE", "false")
        assert Settings().cookie_secure is False

    def test_zero_is_false(self, monkeypatch):
        monkeypatch.setenv("COOKIE_SECURE", "0")
        assert Settings().cookie_secure is False

    def test_true_string_is_true(self, monkeypatch):
        monkeypatch.setenv("COOKIE_SECURE", "true")
        assert Settings().cookie_secure is True

    def test_one_is_true(self, monkeypatch):
        monkeypatch.setenv("COOKIE_SECURE", "1")
        assert Settings().cookie_secure is True


class TestRuntimeConfigResolver:
    """TRK-303 part 2/7: get_settings() resolves RuntimeConfig DB rows on top of
    the env/default Settings, cached behind a monotonic TTL.

    The resolver builds its OWN short-lived engine from ``Settings().postgres_url``
    rather than reusing ``db/session.py``'s ``get_engine()`` — that helper calls
    ``get_settings()`` internally, so reusing it would recurse infinitely.

    NOTE on the DB fixtures below: the suite's shared ``engine`` fixture is
    ``sqlite:///:memory:``, and every ``create_engine("sqlite:///:memory:")`` call
    gets its OWN private database. Since the resolver constructs its own engine
    from the DSN, an in-memory DB can never be observed by it. These tests
    therefore point POSTGRES_URL at a temp *file* sqlite DB so the resolver's
    engine and the test's engine really are the same database.
    """

    @staticmethod
    def _sqlite_db(tmp_path, monkeypatch):
        """Point POSTGRES_URL at a fresh temp-file sqlite DB; return its engine."""
        from sqlalchemy import create_engine

        from infra_brain.config import get_settings

        url = f"sqlite:///{tmp_path / 'runtime_config_test.db'}"
        monkeypatch.setenv("POSTGRES_URL", url)
        get_settings.cache_clear()
        return create_engine(url)

    def test_db_override_wins_over_env_default(self, tmp_path, monkeypatch):
        from sqlalchemy.orm import Session as _Session

        from infra_brain.config import Settings, get_settings
        from infra_brain.db.models import RuntimeConfig

        eng = self._sqlite_db(tmp_path, monkeypatch)
        RuntimeConfig.__table__.create(eng)

        # Sanity: the env/default value is not the override value.
        assert Settings().netdiscovery_tier2_chunk_size == 3

        with _Session(eng) as s:
            s.add(
                RuntimeConfig(
                    key="netdiscovery_tier2_chunk_size",
                    value_type="int",
                    value="750",
                    category="tuning",
                )
            )
            s.commit()

        get_settings.cache_clear()
        assert get_settings().netdiscovery_tier2_chunk_size == 750

        # Clearing the override restores the env/default value.
        with _Session(eng) as s:
            s.query(RuntimeConfig).delete()
            s.commit()
        get_settings.cache_clear()
        assert get_settings().netdiscovery_tier2_chunk_size == 3

    def test_value_types_are_coerced(self, tmp_path, monkeypatch):
        from sqlalchemy.orm import Session as _Session

        from infra_brain.config import get_settings
        from infra_brain.db.models import RuntimeConfig

        eng = self._sqlite_db(tmp_path, monkeypatch)
        RuntimeConfig.__table__.create(eng)
        with _Session(eng) as s:
            s.add_all(
                [
                    RuntimeConfig(key="api_page_size", value_type="int", value="42"),
                    RuntimeConfig(
                        key="integration_confidence_gate", value_type="float", value="0.9"
                    ),
                    RuntimeConfig(key="aws_enabled", value_type="bool", value="true"),
                    RuntimeConfig(key="k8s_cluster_name", value_type="str", value="prod-1"),
                ]
            )
            s.commit()

        get_settings.cache_clear()
        s = get_settings()
        assert s.api_page_size == 42
        assert s.integration_confidence_gate == 0.9
        assert s.aws_enabled is True
        assert s.k8s_cluster_name == "prod-1"

    def test_malformed_and_unknown_overrides_are_ignored(self, tmp_path, monkeypatch):
        """A bad value or a key that is not a real Settings field must be dropped,
        not crash config resolution and not be grafted onto the Settings object.

        ``model_copy(update=...)`` does NOT validate, so an unfiltered key would
        silently become a bogus attribute — hence the explicit field allow-list.
        """
        from sqlalchemy.orm import Session as _Session

        from infra_brain.config import get_settings
        from infra_brain.db.models import RuntimeConfig

        eng = self._sqlite_db(tmp_path, monkeypatch)
        RuntimeConfig.__table__.create(eng)
        with _Session(eng) as s:
            s.add_all(
                [
                    RuntimeConfig(key="api_page_size", value_type="int", value="not-an-int"),
                    RuntimeConfig(key="not_a_real_setting", value_type="str", value="x"),
                    RuntimeConfig(key="k8s_cluster_name", value_type="str", value=None),
                ]
            )
            s.commit()

        get_settings.cache_clear()
        resolved = get_settings()
        assert resolved.api_page_size == 100  # unchanged default
        assert not hasattr(resolved, "not_a_real_setting")
        assert resolved.k8s_cluster_name == ""

    def test_missing_table_degrades_gracefully(self, tmp_path, monkeypatch):
        """Pre-migration (table absent) must return plain env/default Settings."""
        from infra_brain.config import get_settings

        self._sqlite_db(tmp_path, monkeypatch)  # DB exists, runtime_config does not
        get_settings.cache_clear()
        assert get_settings().netdiscovery_tier2_chunk_size == 3

    def test_unreachable_db_degrades_gracefully(self, monkeypatch):
        """A DB that cannot even be connected to must not raise out of get_settings()."""
        import sqlalchemy

        from infra_brain.config import get_settings

        def _boom(*a, **kw):
            raise sqlalchemy.exc.OperationalError("connect", {}, Exception("unreachable"))

        monkeypatch.setattr(sqlalchemy, "create_engine", _boom)
        get_settings.cache_clear()
        assert get_settings().netdiscovery_tier2_chunk_size == 3

    def test_ttl_window_does_not_requery(self, monkeypatch):
        """Within the TTL window a second get_settings() must reuse the cache —
        proves the cache is actually caching, not querying the DB every call."""
        import infra_brain.config as cfg

        calls = []

        def _spy(base=None):
            calls.append(1)
            return {}

        monkeypatch.setattr(cfg, "_load_runtime_overrides", _spy)
        monkeypatch.setattr(cfg, "_RUNTIME_CONFIG_TTL_SECONDS", 3600.0)
        cfg.get_settings.cache_clear()

        first = cfg.get_settings()
        second = cfg.get_settings()
        assert len(calls) == 1
        assert first is second

    def test_expired_ttl_requeries(self, monkeypatch):
        import infra_brain.config as cfg

        calls = []

        def _spy(base=None):
            calls.append(1)
            return {}

        monkeypatch.setattr(cfg, "_load_runtime_overrides", _spy)
        monkeypatch.setattr(cfg, "_RUNTIME_CONFIG_TTL_SECONDS", -1.0)
        cfg.get_settings.cache_clear()

        cfg.get_settings()
        cfg.get_settings()
        assert len(calls) == 2

    def test_cache_clear_still_works_as_before(self):
        """secrets.py:73 calls get_settings.cache_clear() directly after loading
        Bitwarden secrets into os.environ — that call site must keep working."""
        from infra_brain.config import get_settings

        get_settings()
        get_settings.cache_clear()  # must not raise AttributeError
        assert get_settings() is not None

    def test_cache_clear_picks_up_env_change(self, monkeypatch):
        """The pre-existing lru_cache contract: cache_clear() then a new env value."""
        from infra_brain.config import get_settings

        monkeypatch.setenv("AWS_ENABLED", "1")
        get_settings.cache_clear()
        assert get_settings().aws_enabled is True
        get_settings.cache_clear()

    def test_override_type_is_validated_against_the_field_annotation(self, tmp_path, monkeypatch):
        """A row's self-declared ``value_type`` must NOT be trusted as the coercion
        rule — the Settings field's real annotation is the authority.

        ``model_copy(update=...)`` does not validate, so trusting ``value_type``
        would let one mistyped row put a str on an int field process-wide:
            Settings().model_copy(update={'api_page_size': 'nope'}).api_page_size
              -> 'nope'   (str, on an int field)
        which then explodes somewhere unrelated up to a TTL later.
        """
        from sqlalchemy.orm import Session as _Session

        from infra_brain.config import get_settings
        from infra_brain.db.models import RuntimeConfig

        eng = self._sqlite_db(tmp_path, monkeypatch)
        RuntimeConfig.__table__.create(eng)
        with _Session(eng) as s:
            s.add_all(
                [
                    # value_type lies ("str"), value is not a valid int -> DROPPED
                    RuntimeConfig(
                        key="api_page_size", value_type="str", value="totally-not-an-int"
                    ),
                    # value_type lies ("str"), value is not a valid bool -> DROPPED
                    RuntimeConfig(key="aws_enabled", value_type="str", value="yes-string"),
                    # value_type lies ("str") but the value IS a valid int for the
                    # field's annotation -> APPLIED, and coerced to real int
                    RuntimeConfig(key="api_timeout_seconds", value_type="str", value="55"),
                ]
            )
            s.commit()

        get_settings.cache_clear()
        resolved = get_settings()

        assert resolved.api_page_size == 100
        assert isinstance(resolved.api_page_size, int)
        assert resolved.aws_enabled is False
        assert isinstance(resolved.aws_enabled, bool)
        # annotation wins over the lying value_type, and yields the real type
        assert resolved.api_timeout_seconds == 55
        assert isinstance(resolved.api_timeout_seconds, int)

    def test_postgres_engine_gets_connect_and_statement_timeouts(self, monkeypatch):
        """Unbounded blocking DB I/O on the event loop is the risk here.
        connect_timeout bounds the handshake; statement_timeout bounds the query
        once the connection is accepted. Both must be set for postgres."""
        import sqlalchemy

        import infra_brain.config as cfg

        seen = {}

        def _fake_create_engine(url, **kwargs):
            seen["url"] = url
            seen["connect_args"] = kwargs.get("connect_args")
            raise RuntimeError("stop here — we only care about the engine kwargs")

        monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@db:5432/x")
        monkeypatch.setattr(sqlalchemy, "create_engine", _fake_create_engine)

        assert cfg._load_runtime_overrides() == {}  # degrades, does not raise
        ca = seen["connect_args"]
        assert ca["connect_timeout"] == cfg._RUNTIME_CONFIG_CONNECT_TIMEOUT_S
        assert ca["options"] == f"-c statement_timeout={cfg._RUNTIME_CONFIG_STATEMENT_TIMEOUT_MS}"

    def test_stale_value_served_without_blocking_on_a_busy_refresh(self):
        """Serve-stale-while-revalidating: with the TTL expired and another thread
        already refreshing, a caller must get the previous value immediately
        rather than queueing behind that thread's DB I/O.

        This is what stops an expired TTL from stalling every in-flight request in
        the process (get_settings() is called synchronously from async handlers).
        """
        import threading
        import time as _time

        import infra_brain.config as cfg

        original_ttl = cfg._RUNTIME_CONFIG_TTL_SECONDS
        original_loader = cfg._load_runtime_overrides
        try:
            cfg._load_runtime_overrides = lambda base=None: {}
            cfg._RUNTIME_CONFIG_TTL_SECONDS = 3600.0
            cfg.get_settings.cache_clear()
            warm = cfg.get_settings()  # populate the cache

            cfg._RUNTIME_CONFIG_TTL_SECONDS = -1.0  # everything is now stale

            holding = threading.Event()
            release = threading.Event()

            def _hog():
                with cfg._settings_lock:
                    holding.set()
                    release.wait(5)

            t = threading.Thread(target=_hog, daemon=True)
            t.start()
            assert holding.wait(5), "helper thread never took the lock"

            started = _time.monotonic()
            served = cfg.get_settings()
            elapsed = _time.monotonic() - started

            release.set()
            t.join(5)

            assert served is warm, "should have served the stale cached value"
            assert elapsed < 1.0, f"blocked on the busy refresh for {elapsed:.2f}s"
        finally:
            cfg._load_runtime_overrides = original_loader
            cfg._RUNTIME_CONFIG_TTL_SECONDS = original_ttl
            cfg.get_settings.cache_clear()

    def test_cache_clear_does_not_deadlock_during_a_refresh(self):
        """secrets.py calls cache_clear() after mutating os.environ. If that ever
        happens on the same thread that is mid-refresh, a non-reentrant lock would
        deadlock. The lock is an RLock specifically to make this safe."""
        import infra_brain.config as cfg

        original_loader = cfg._load_runtime_overrides
        try:

            def _clears_during_refresh(base=None):
                cfg.get_settings.cache_clear()  # same thread, lock already held
                return {}

            cfg._load_runtime_overrides = _clears_during_refresh
            cfg.get_settings.cache_clear()
            assert cfg.get_settings() is not None  # would hang forever on a plain Lock
        finally:
            cfg._load_runtime_overrides = original_loader
            cfg.get_settings.cache_clear()

    def test_cache_and_timestamp_are_read_atomically(self):
        """The cached Settings and its timestamp live in one tuple, so a reader can
        never observe a new value paired with an old timestamp (or vice versa)."""
        import infra_brain.config as cfg

        cfg.get_settings.cache_clear()
        assert cfg._settings_cache is None
        cfg.get_settings()
        snapshot = cfg._settings_cache
        assert isinstance(snapshot, tuple) and len(snapshot) == 2
        assert isinstance(snapshot[0], Settings)
        assert isinstance(snapshot[1], float)
        assert not hasattr(cfg, "_settings_cache_at"), (
            "the separate timestamp global must be gone — that was the torn-read source"
        )

    def test_resolver_does_not_use_db_session_engine(self, monkeypatch):
        """Guard against reintroducing the recursion: db/session.py's get_engine()
        calls get_settings(), so the resolver must never call it."""
        import infra_brain.db.session as dbsession
        from infra_brain.config import get_settings

        def _forbidden(*a, **kw):
            raise AssertionError("resolver must not call db.session.get_engine()")

        monkeypatch.setattr(dbsession, "get_engine", _forbidden)
        monkeypatch.setattr(dbsession, "get_readonly_engine", _forbidden)
        get_settings.cache_clear()
        assert get_settings() is not None


class TestInfraBrainDevEmptyString:
    """Regression tests for INFRA_BRAIN_DEV empty-string crash (docker-compose
    injects ``INFRA_BRAIN_DEV=`` when the host var is unset via ``${VAR:-}``).

    Before the fix, pydantic raised a ValidationError for ``""`` → crash loop.
    """

    def test_empty_string_coerces_to_false(self, monkeypatch):
        """``INFRA_BRAIN_DEV=`` (empty string from docker-compose) must not crash."""
        monkeypatch.setenv("INFRA_BRAIN_DEV", "")
        s = Settings()
        assert s.infra_brain_dev is False

    def test_none_env_unset_is_false(self, monkeypatch):
        """Unset env var (no env file entry) must yield the default False."""
        monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
        s = Settings()
        assert s.infra_brain_dev is False

    def test_truthy_values_still_work(self, monkeypatch):
        """True-ish strings must still parse as True."""
        for val in ("1", "true", "True", "TRUE", "yes", "on"):
            monkeypatch.setenv("INFRA_BRAIN_DEV", val)
            s = Settings()
            assert s.infra_brain_dev is True, f"Expected True for INFRA_BRAIN_DEV={val!r}"

    def test_falsy_values_still_work(self, monkeypatch):
        """Explicit false-ish strings must still parse as False."""
        for val in ("0", "false", "False", "FALSE", "no", "off"):
            monkeypatch.setenv("INFRA_BRAIN_DEV", val)
            s = Settings()
            assert s.infra_brain_dev is False, f"Expected False for INFRA_BRAIN_DEV={val!r}"
