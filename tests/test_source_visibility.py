"""The UI source-visibility rule, and the endpoint that applies it.

DELIBERATELY FICTIONAL DOMAINS. Every test of the rule itself uses invented
domain names (``flux_capacitor``, ``tachyon_grid``, …) rather than the real
roster. The rule is a pure function of (retired, row_count); pinning it to
``vsphere`` or ``identity`` would mean these tests start failing the day someone
legitimately revives a collector — testing today's configuration instead of the
policy. The endpoint tests below seed their own rows for the same reason.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.api.source_visibility import (
    SourceVisibility,
    source_visibility,
    visibility_reason,
)
from infra_brain.config import get_settings
from infra_brain.db.models import CollectionRun, Resource
from tests.support.pg import make_engine

# ---------------------------------------------------------------------------
# The rule — all four cases
# ---------------------------------------------------------------------------


def test_live_collector_with_data_is_visible():
    assert source_visibility(retired=False, row_count=42) is SourceVisibility.VISIBLE


def test_live_collector_with_zero_data_is_still_visible():
    """The case that must NOT collapse into `hidden`.

    A live collector holding nothing has either never run or found nothing —
    both are states an operator needs to see. Hiding it would hide the evidence
    that a source they intend to use is silently producing no data.
    """
    assert source_visibility(retired=False, row_count=0) is SourceVisibility.VISIBLE


def test_retired_collector_with_data_is_historical_not_hidden():
    """Real collected rows outlive the decision to stop collecting.

    Hiding the only route to data that exists would make the UI assert, by
    omission, something false. It is shown and marked historical instead.
    """
    assert source_visibility(retired=True, row_count=53) is SourceVisibility.HISTORICAL


def test_retired_collector_with_zero_data_is_hidden():
    assert source_visibility(retired=True, row_count=0) is SourceVisibility.HIDDEN


def test_negative_row_count_cannot_unhide_a_retired_source():
    """A bad count is treated as zero, never as a reason to show something."""
    assert source_visibility(retired=True, row_count=-1) is SourceVisibility.HIDDEN


def test_visibility_values_are_the_wire_strings():
    """The frontend compares against these literals; they are the contract."""
    assert SourceVisibility.VISIBLE.value == "visible"
    assert SourceVisibility.HISTORICAL.value == "historical"
    assert SourceVisibility.HIDDEN.value == "hidden"


@pytest.mark.parametrize(
    ("retired", "row_count"),
    [(False, 0), (False, 7), (True, 0), (True, 7)],
)
def test_every_case_has_a_nonempty_reason(retired, row_count):
    assert visibility_reason(retired=retired, row_count=row_count).strip()


def test_reason_for_historical_names_the_row_count():
    assert "53" in visibility_reason(retired=True, row_count=53)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

#: Fictional roster injected in place of `agent_specs()`. Two live, two retired
#: — one of each retired kind — so the endpoint exercises all three verdicts
#: without depending on what the real registry happens to hold today.
_FAKE_LIVE = ("flux_capacitor", "tachyon_grid")
_FAKE_RETIRED_WITH_DATA = "orbital_mainframe"
_FAKE_RETIRED_EMPTY = "steam_telegraph"


class _FakeSpec:
    def __init__(self, retired: bool) -> None:
        self.retired = retired


def _fake_specs() -> dict:
    return {
        _FAKE_LIVE[0]: _FakeSpec(False),
        _FAKE_LIVE[1]: _FakeSpec(False),
        _FAKE_RETIRED_WITH_DATA: _FakeSpec(True),
        _FAKE_RETIRED_EMPTY: _FakeSpec(True),
    }


def _fake_retired() -> set[str]:
    return {_FAKE_RETIRED_WITH_DATA, _FAKE_RETIRED_EMPTY}


@pytest.fixture
def engine():
    return make_engine()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.sources import sources_router

    app = FastAPI()
    app.include_router(sources_router)
    with (
        patch("infra_brain.api.routers.sources.get_session", _get_session),
        patch("infra_brain.etl.spec.agent_specs", _fake_specs),
        patch("infra_brain.etl.spec.retired_domains", _fake_retired),
    ):
        yield TestClient(app)


def _seed(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        # flux_capacitor: live, has data, has a successful run.
        s.add(Resource(domain=_FAKE_LIVE[0], type="host", name="fc-01", source="test"))
        s.add(
            CollectionRun(
                domain=_FAKE_LIVE[0],
                trigger_type="scheduled",
                status="completed",
                started_at=now - timedelta(hours=1),
            )
        )
        # tachyon_grid: live, NO data, and only a failed run — must still be visible.
        s.add(
            CollectionRun(
                domain=_FAKE_LIVE[1],
                trigger_type="scheduled",
                status="failed",
                started_at=now - timedelta(hours=2),
            )
        )
        # orbital_mainframe: retired, but two real rows survive.
        for n in ("om-01", "om-02"):
            s.add(Resource(domain=_FAKE_RETIRED_WITH_DATA, type="host", name=n, source="test"))
        # steam_telegraph: retired, nothing at all.
        s.commit()


def _by_domain(payload) -> dict:
    return {item["domain"]: item for item in payload["items"]}


def test_endpoint_returns_a_page_envelope(client, engine):
    _seed(engine)
    r = client.get("/api/dashboard/sources")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "limit", "offset", "by_visibility"}
    assert body["total"] == 4


def test_endpoint_shape_per_item(client, engine):
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_LIVE[0]]
    assert set(item) == {
        "domain",
        "retired",
        "live",
        "configured",
        "missing_config",
        "resource_count",
        "last_run_at",
        "last_success_at",
        "visibility",
        "visibility_reason",
    }


def test_live_domain_reported_visible_with_its_counts(client, engine):
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_LIVE[0]]
    assert item["visibility"] == "visible"
    assert item["retired"] is False
    assert item["live"] is True
    assert item["resource_count"] == 1
    assert item["last_success_at"] is not None


def test_live_domain_with_no_data_and_no_success_is_still_visible(client, engine):
    """Regression guard for the whole point of the `live` branch."""
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_LIVE[1]]
    assert item["visibility"] == "visible"
    assert item["resource_count"] == 0
    assert item["last_run_at"] is not None
    assert item["last_success_at"] is None


def test_retired_domain_with_data_is_historical(client, engine):
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_RETIRED_WITH_DATA]
    assert item["visibility"] == "historical"
    assert item["retired"] is True
    assert item["resource_count"] == 2


def test_retired_domain_with_no_data_is_hidden(client, engine):
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_RETIRED_EMPTY]
    assert item["visibility"] == "hidden"
    assert item["resource_count"] == 0


def test_by_visibility_counts_the_whole_inventory(client, engine):
    _seed(engine)
    body = client.get("/api/dashboard/sources").json()
    assert body["by_visibility"] == {"visible": 2, "historical": 1, "hidden": 1}


def test_by_visibility_is_not_narrowed_by_paging(client, engine):
    """Paged subtotals would misstate a 'N sources hidden' affordance."""
    _seed(engine)
    body = client.get("/api/dashboard/sources?limit=1").json()
    assert len(body["items"]) == 1
    assert body["total"] == 4
    assert body["by_visibility"] == {"visible": 2, "historical": 1, "hidden": 1}


def test_roster_is_derived_from_specs_not_hardcoded(client, engine):
    """The fictional domains only appear because `agent_specs()` was patched.

    If this endpoint ever grows a literal domain list, the real roster leaks
    into the response and this assertion fails.
    """
    _seed(engine)
    domains = set(_by_domain(client.get("/api/dashboard/sources").json()))
    assert domains == {*_FAKE_LIVE, _FAKE_RETIRED_WITH_DATA, _FAKE_RETIRED_EMPTY}


def test_domain_with_no_declared_requirements_reports_configured(client, engine):
    """`_DOMAIN_REQUIREMENTS` has no entry for these fictional domains."""
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_LIVE[0]]
    assert item["configured"] is True
    assert item["missing_config"] == []


def test_missing_config_reports_keys_never_values(client, engine, monkeypatch):
    """The endpoint must be safe to render verbatim — keys only, no secrets."""
    from infra_brain.api import _helpers

    monkeypatch.setitem(
        _helpers._DOMAIN_REQUIREMENTS,
        _FAKE_LIVE[0],
        [{"key": "MADE_UP_TOKEN", "label": "Made-up token", "attr": "definitely_not_a_setting"}],
    )
    _seed(engine)
    item = _by_domain(client.get("/api/dashboard/sources").json())[_FAKE_LIVE[0]]
    assert item["configured"] is False
    assert item["missing_config"] == ["MADE_UP_TOKEN"]
