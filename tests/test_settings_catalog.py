"""Settings catalog: shape, source attribution, and write-path enforcement.

The secret-withholding contract lives in its own file
(`tests/test_settings_catalog_secrets.py`) — that one is the hard constraint
and is deliberately not mixed in with the functional coverage here.

What this file pins:

* SHAPE — the catalog is DERIVED from the live `Settings` model, so it covers
  every field, with real types, real defaults, and descriptions harvested from
  config.py's own comments. A hardcoded mirror would drift; these tests fail if
  someone replaces the derivation with one.
* SOURCE ATTRIBUTION — the TRK-314 feature. All three sources (`default`,
  `env`, `db-override`) are exercised, plus the two things that made TRK-314
  invisible: `shadowed_value` (what an applied override is masking) and
  `override_ignored_reason` (a row that exists but did nothing).
* WRITE ENFORCEMENT — every editability rule the UI renders is re-checked
  server-side. UI read-only-ness is not authorization.
* REVERT TO DEFAULT — deleting the row and reporting what took over.
"""

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from infra_brain.config import Settings, get_settings
from infra_brain.db.models import Base, RuntimeConfig


@contextmanager
def _catalog_client(tmp_path, monkeypatch, db_name="catalog.sqlite3"):
    import infra_brain.api.routers.settings_catalog as mod

    db_path = tmp_path / db_name
    dsn = f"sqlite:///{db_path}"
    eng = create_engine(dsn)
    Base.metadata.create_all(eng)

    monkeypatch.setenv("POSTGRES_URL", dsn)
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr(mod, "get_session", _get_session)

    app = FastAPI()
    app.include_router(mod.settings_catalog_router)
    try:
        yield TestClient(app), eng, mod
    finally:
        eng.dispose()
        get_settings.cache_clear()


def _entry(body, key):
    return next(it for it in body["items"] if it["key"] == key)


def _seed(eng, key, value, **kw):
    with Session(eng) as s:
        s.add(RuntimeConfig(key=key, value=value, value_type=kw.pop("value_type", "str"), **kw))
        s.commit()


# ---------------------------------------------------------------------------
# Shape — derived, not transcribed
# ---------------------------------------------------------------------------


def test_catalog_covers_every_settings_field(tmp_path, monkeypatch):
    """Derivation check. A hand-maintained list could not stay exhaustive."""
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        body = client.get("/api/dashboard/settings-catalog").json()
        assert {it["key"] for it in body["items"]} == set(Settings.model_fields)
        assert body["total"] == len(Settings.model_fields)


def test_catalog_entry_carries_type_default_group_and_env_var(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        body = client.get("/api/dashboard/settings-catalog").json()

        pool = _entry(body, "db_pool_size")
        assert pool["type"] == "int"
        assert pool["default"] == "5"
        assert pool["env_var"] == "DB_POOL_SIZE"
        assert pool["group"] == "Database & cache"
        assert pool["editable"] is True

        # bool renders lowercase — what is shown is what you would type back in.
        flag = _entry(body, "sweep_graph_enabled")
        assert flag["type"] == "bool"
        assert flag["default"] in ("true", "false")


def test_descriptions_are_harvested_from_config_source_comments(tmp_path, monkeypatch):
    """config.py documents fields with `#` comments, not Field(description=...).

    0 of the ~270 fields carry a pydantic description, so a catalog that read
    only `FieldInfo.description` would ship entirely blank. This asserts the
    comment harvesting actually produced text for a decent share of fields, and
    that a specific, stable one came through.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        items = client.get("/api/dashboard/settings-catalog").json()["items"]
        described = [it for it in items if it["description"]]
        assert len(described) > len(items) // 2, "comment harvesting produced almost nothing"

        pool = _entry({"items": items}, "db_pool_size")
        assert "pool" in pool["description"].lower()


def test_groups_are_returned_in_display_order_and_all_used(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, mod):
        body = client.get("/api/dashboard/settings-catalog").json()
        assert body["groups"], "no groups returned"
        # Every group named in the envelope is actually used by some entry, and
        # ordering follows the module's declared order.
        used = {it["group"] for it in body["items"]}
        assert set(body["groups"]) == used
        assert body["groups"] == [g for g in mod._GROUP_ORDER if g in used]


def test_dispatchable_pause_levers_are_not_in_the_catalog(tmp_path, monkeypatch):
    """They are not Settings fields; they belong to the Agents page.

    Surfacing them here would let someone un-pause a collector from a panel
    that does not explain what pausing means.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        _seed(eng, "dispatchable__linux", "false")
        body = client.get("/api/dashboard/settings-catalog").json()
        assert not [it for it in body["items"] if it["key"].startswith("dispatchable__")]


# ---------------------------------------------------------------------------
# Source attribution — all three, plus the two TRK-314 tells
# ---------------------------------------------------------------------------


def test_source_default_when_nothing_overrides_it(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        monkeypatch.delenv("DB_POOL_SIZE", raising=False)
        get_settings.cache_clear()
        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "db_pool_size")
        assert row["source"] == "default"
        assert row["value"] == "5"
        assert row["db_row"] is False
        assert row["shadowed_value"] is None


def test_source_env_when_the_environment_sets_it(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        monkeypatch.setenv("DB_POOL_SIZE", "17")
        get_settings.cache_clear()
        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "db_pool_size")
        assert row["source"] == "env"
        assert row["value"] == "17"
        assert row["db_row"] is False


def test_source_db_override_and_the_env_value_it_is_masking(tmp_path, monkeypatch):
    """TRK-314 in one assertion.

    A runtime_config row silently winning over a wrong .env value, with nothing
    in any UI showing that was happening, is the bug. The catalog reports both
    the winner and the loser.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        monkeypatch.setenv("DB_POOL_SIZE", "17")
        get_settings.cache_clear()
        _seed(eng, "db_pool_size", "33", value_type="int")

        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "db_pool_size")
        assert row["source"] == "db-override"
        assert row["value"] == "33"
        assert row["shadowed_value"] == "17"
        assert row["db_row"] is True
        assert row["override_ignored_reason"] is None


def test_denylisted_row_is_reported_as_present_but_ignored(tmp_path, monkeypatch):
    """The inverse silent no-op: a row that exists and does nothing.

    Before this, the only evidence was a log line per TTL poll.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        _seed(eng, "scan_readonly_enforce", "false", value_type="bool")
        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "scan_readonly_enforce")
        assert row["db_row"] is True
        assert row["source"] != "db-override"
        assert "IGNORED" in row["override_ignored_reason"]
        assert row["editable"] is False
        assert "Non-overridable" in row["locked_reason"]


def test_invalid_row_is_reported_as_present_but_not_applied(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        _seed(eng, "db_pool_size", "not-a-number", value_type="int")
        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "db_pool_size")
        assert row["db_row"] is True
        assert row["source"] == "default"
        assert "NOT applied" in row["override_ignored_reason"]


# ---------------------------------------------------------------------------
# Write path — server-side enforcement (never trust the UI)
# ---------------------------------------------------------------------------


def test_put_editable_field_persists_and_flips_the_source(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put("/api/dashboard/settings-catalog/db_pool_size", json={"value": "23"})
        assert resp.status_code == 200
        out = resp.json()
        assert out["value"] == "23"
        assert out["source"] == "db-override"

        with Session(eng) as s:
            row = s.get(RuntimeConfig, "db_pool_size")
            assert row.value == "23"
            assert row.value_type == "int"  # derived from the field annotation
            assert row.is_secret is False


def test_put_rejects_a_key_that_is_not_a_settings_field(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put(
            "/api/dashboard/settings-catalog/dispatchable__linux", json={"value": "false"}
        )
        assert resp.status_code == 404
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "dispatchable__linux") is None


def test_put_rejects_a_denylisted_field(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put(
            "/api/dashboard/settings-catalog/dlp_fail_closed", json={"value": "false"}
        )
        assert resp.status_code == 403
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "dlp_fail_closed") is None


def test_put_rejects_a_secret_field_with_a_pointer_to_where_it_belongs(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put("/api/dashboard/settings-catalog/gitlab_token", json={"value": "glpat-x"})
        assert resp.status_code == 403
        assert "Bitwarden" in resp.json()["detail"]
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "gitlab_token") is None


def test_put_rejects_a_wrongly_typed_value(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put("/api/dashboard/settings-catalog/db_pool_size", json={"value": "twenty"})
        assert resp.status_code == 400
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "db_pool_size") is None


# ---------------------------------------------------------------------------
# Revert to default
# ---------------------------------------------------------------------------


def test_delete_reverts_to_the_env_value_and_reports_what_took_over(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        monkeypatch.setenv("DB_POOL_SIZE", "17")
        get_settings.cache_clear()
        assert (
            client.put("/api/dashboard/settings-catalog/db_pool_size", json={"value": "33"})
        ).status_code == 200

        resp = client.delete("/api/dashboard/settings-catalog/db_pool_size")
        assert resp.status_code == 200
        out = resp.json()
        assert out["source"] == "env"
        assert out["value"] == "17"
        assert out["db_row"] is False
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "db_pool_size") is None


def test_delete_without_an_override_is_404(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        assert client.delete("/api/dashboard/settings-catalog/db_pool_size").status_code == 404


def test_delete_is_refused_for_a_non_editable_field(tmp_path, monkeypatch):
    """A denylisted row must not be removable from THIS surface either.

    Not because removing it is dangerous (it does nothing), but because the
    catalog advertises the field as locked and the server must mean it. The raw
    runtime-config editor remains the deliberate escape hatch.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        _seed(eng, "dlp_fail_closed", "false", value_type="bool")
        assert client.delete("/api/dashboard/settings-catalog/dlp_fail_closed").status_code == 403
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "dlp_fail_closed") is not None
