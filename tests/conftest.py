import pytest
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv():
    """Stop Settings from reading the developer's local .env during tests.

    CI has no .env, so the suite runs against pure field defaults there. A
    populated local .env (e.g. LLM_PROVIDER=openai, VSPHERE_SSL_VERIFY=false)
    would otherwise leak into Settings() and make local runs diverge from CI.
    Tests that need specific values set them via monkeypatch (os.environ), which
    pydantic still honors — only the .env *file* is disabled here.

    Function-scoped (not session): test_config.test_override_via_env reloads the
    config module, which rebuilds the Settings class with env_file=".env" again.
    Re-applying per test keeps isolation intact regardless of reload order.
    """
    import os

    from infra_brain.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    # postgres_url is now REQUIRED (no production localhost default — an empty DSN
    # must abort loud). The suite runs with no .env, so supply a harmless SQLite
    # test DSN via the env var pydantic reads, unless a test set its own (e.g.
    # test_e2e_smoke points POSTGRES_URL at a temp SQLite file). The value still
    # flows through the single postgres_url field — no production code default.
    _had_pg = "POSTGRES_URL" in os.environ
    if not _had_pg:
        os.environ["POSTGRES_URL"] = "sqlite:///:memory:"
    get_settings.cache_clear()
    yield
    if not _had_pg:
        os.environ.pop("POSTGRES_URL", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _strong_cookie_secret_baseline(monkeypatch):
    """Global baseline: give every test a strong UI_COOKIE_SECRET.

    Item 1.5b (F-027/F-031) made `create_app()` hard-refuse to start with a
    missing/weak UI_COOKIE_SECRET (see assert_cookie_secret_ok in
    dashboard_auth.py). Without a baseline, any test that builds the real app
    via main.create_app() — or otherwise triggers that check — errors out
    with RuntimeError before the test body even runs.

    This fixture ONLY supplies the cookie secret. It deliberately does NOT
    set INFRA_BRAIN_DEV — auth must stay REAL by default across the suite so
    the fail-closed behavior introduced by this hardening item is exercised,
    not masked. Tests that need the explicit dev-mode bypass set
    INFRA_BRAIN_DEV=1 themselves in their own fixtures.

    Runs via monkeypatch (auto-restores) and clears the settings cache so
    get_settings() picks up the change. Tests that set/unset UI_COOKIE_SECRET
    themselves via their own monkeypatch calls override this baseline, since
    the last-applied env value wins and each test clears the cache again
    around its own patch.
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("UI_COOKIE_SECRET", "test-cookie-secret-not-weak-0123456789")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _engine_scope(fixture_name, config):  # noqa: ARG001 - pytest dynamic-scope hook
    """Session-scoped on SQLite (unchanged); per-test on real PostgreSQL.

    The SQLite engine is a private in-memory DB nobody else can reach, so one
    per session is free and its cross-test row accumulation is long-standing
    behaviour. In ``PG_GATE_DSN`` mode every helper shares ONE PostgreSQL, and
    ``make_engine()`` wipes it on handout — a session-scoped engine there would
    hand back a database some other module had since truncated. Per-test scope
    makes each handout the reset point, which is also stricter isolation.
    """
    from tests.support.pg import pg_enabled

    return "function" if pg_enabled() else "session"


@pytest.fixture(scope=_engine_scope)
def engine():
    from tests.support.pg import make_engine

    return make_engine()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session
        session.rollback()
