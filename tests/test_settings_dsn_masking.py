"""H-1 regression tests — DB/Redis DSN credentials must never leave the
``GET /api/dashboard/settings`` route in cleartext.

``_setting_row`` masked by FIELD NAME only, against ``_SECRET_HINTS =
("key", "token", "password", "secret")``. ``postgres_url``,
``postgres_readonly_url`` and ``redis_url`` are plain ``str`` (not
``SecretStr``), carry ``scheme://user:password@host`` DSNs, and match NONE of
those hints — so the whole DSN, password included, rendered as a type "text"
row under the "Database" group for any logged-in session.

These tests assert the REAL behaviour (the password byte-string is absent from
the response) rather than the shape of the fix.
"""

from __future__ import annotations

from infra_brain.api._helpers import _setting_row

DSN_PASSWORD = "hunter2SuperSecret"  # noqa: S105 — test fixture, not a real credential
PG_DSN = f"postgresql+psycopg://ibuser:{DSN_PASSWORD}@db.internal:5432/infra_brain"
REDIS_DSN = f"redis://:{DSN_PASSWORD}@redis.internal:6379/0"


class TestSettingRowDsnMasking:
    """Unit-level: _setting_row must scrub DSN credentials regardless of key name."""

    def test_postgres_url_password_is_not_rendered(self):
        row = _setting_row("postgres_url", PG_DSN)
        rendered = f"{row.v or ''}"
        assert DSN_PASSWORD not in rendered
        assert "ibuser" not in rendered

    def test_postgres_readonly_url_password_is_not_rendered(self):
        row = _setting_row("postgres_readonly_url", PG_DSN)
        assert DSN_PASSWORD not in f"{row.v or ''}"

    def test_redis_url_password_is_not_rendered(self):
        row = _setting_row("redis_url", REDIS_DSN)
        assert DSN_PASSWORD not in f"{row.v or ''}"

    def test_host_and_scheme_survive_so_the_row_stays_useful(self):
        """Scrubbing is scoped to the credentials segment — operators must
        still be able to see WHICH database is configured."""
        row = _setting_row("postgres_url", PG_DSN)
        assert "db.internal:5432/infra_brain" in (row.v or "")
        assert (row.v or "").startswith("postgresql+psycopg://")

    def test_credential_free_url_is_untouched(self):
        """A plain URL with no embedded credentials must render verbatim —
        over-redacting every 'scheme://' would blind operators for no gain."""
        row = _setting_row("gitlab_url", "https://gitlab.example.com")
        assert row.v == "https://gitlab.example.com"

    def test_credential_free_redis_url_is_untouched(self):
        row = _setting_row("redis_url", "redis://localhost:6379/0")
        assert row.v == "redis://localhost:6379/0"

    def test_bool_rows_are_unaffected(self):
        row = _setting_row("dlp_fail_closed", True)
        assert row.type == "bool"
        assert row.on is True

    def test_name_hinted_secret_still_masked(self):
        """The pre-existing FIELD-NAME masking must keep working."""
        row = _setting_row("anthropic_api_key", "sk-supersecret-1234")
        assert row.type == "secret"
        assert "supersecret" not in (row.v or "")


# The end-to-end route-level counterpart lives in
# tests/test_dashboard_api.py::test_settings_never_serves_dsn_credentials
# (it needs that module's dev-mode ``client`` fixture).
