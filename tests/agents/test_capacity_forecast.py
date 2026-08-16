"""Tests for CapacityForecastAgent (GitLab #99)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.agents.capacity_forecast import CapacityForecastAgent
from infra_brain.etl.base import CollectorSkipped


def _make_metric(moref, used_pct, collected_at, name="ds01", vcenter="vc1"):
    m = MagicMock()
    m.moref = moref
    m.name = name
    m.vcenter = vcenter
    m.used_pct = used_pct
    m.collected_at = collected_at
    return m


def _growing_series(moref="datastore-1", n=8, start_pct=50.0, step=2.0, span_days=14):
    """n samples, evenly spaced over span_days, used_pct growing by `step` each."""
    base = datetime.now(UTC) - timedelta(days=span_days)
    out = []
    for i in range(n):
        collected_at = base + timedelta(days=(span_days / (n - 1)) * i)
        out.append(_make_metric(moref, start_pct + step * i, collected_at))
    return out


def _agent(enabled=True, threshold=90.0, retention_days=90):
    agent = CapacityForecastAgent.__new__(CapacityForecastAgent)
    agent.settings = MagicMock(
        capacity_forecast_enabled=enabled,
        capacity_forecast_used_pct_threshold=threshold,
        retention_snapshots_days=retention_days,
    )
    return agent


def _patch_session(rows):
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = rows
    # existing-Instinct lookup: session.query(Instinct).filter_by(...).filter(...).first()
    mock_session.query.return_value.filter_by.return_value.filter.return_value.first.return_value = None
    return mock_session


class TestDomainAndSpec:
    def test_domain_is_capacity_forecast(self):
        assert CapacityForecastAgent.spec.domain == "capacity_forecast"

    def test_callbacks_wired(self):
        """build_callbacks() must run in __init__ — safety layer must be active."""
        with patch("infra_brain.etl.base.build_callbacks", return_value=["cb"]) as mock_build:
            with patch("infra_brain.etl.base.get_settings"):
                agent = CapacityForecastAgent()
        mock_build.assert_called_once()
        assert agent.callbacks == ["cb"]


class TestFlagGating:
    def test_disabled_by_default_is_noop(self):
        """Flag off (default False) — collect() must raise CollectorSkipped
        (status="skipped") without touching the DB, not return []
        (status="completed" with 0 resources -- the whole collector has
        nothing to do, matching backup/dns/net/cloud/container_registry's
        convention for "nothing configured")."""
        agent = _agent(enabled=False)
        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            with pytest.raises(CollectorSkipped):
                agent.collect()
        mock_ctx.assert_not_called()

    def test_missing_settings_flag_defaults_to_disabled(self):
        """A settings object without the attribute at all must behave as off."""
        agent = CapacityForecastAgent.__new__(CapacityForecastAgent)
        agent.settings = object()  # no capacity_forecast_enabled attribute
        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            with pytest.raises(CollectorSkipped):
                agent.collect()
        mock_ctx.assert_not_called()


class TestCollectSuccess:
    def test_growing_datastore_writes_instinct(self):
        """A datastore with a clear upward used_pct trend promotes an Instinct."""
        agent = _agent(enabled=True)
        rows = _growing_series()
        mock_session = _patch_session(rows)

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_session
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = agent.collect()

        assert result == []
        added = mock_session.add.call_args_list
        assert len(added) == 1
        instinct = added[0][0][0]
        assert instinct.domain == "vsphere"
        assert instinct.promoted_by == "capacity_forecast"
        assert 0.05 <= instinct.confidence <= 0.99
        assert "datastore-1" in instinct.citation
        assert "days" not in instinct.pattern.split("~")[0]  # sanity: text before number
        mock_session.commit.assert_called_once()

    def test_flat_datastore_is_not_forecast(self):
        """No growth (flat used_pct) must not produce an Instinct."""
        agent = _agent(enabled=True)
        rows = _growing_series(step=0.0)
        mock_session = _patch_session(rows)

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_session
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = agent.collect()

        assert result == []
        assert mock_session.add.call_args_list == []

    def test_same_moref_different_vcenter_not_merged(self):
        """Two datastores that share a MoRef but belong to different vCenters
        must be forecast independently, not merged into one regression.

        GitLab audit finding (capacity_forecast.py ~line 158): grouping was
        keyed on `moref` alone. MoRefs are only unique *within* a single
        vCenter (VsphereDatastore.UniqueConstraint("vcenter", "moref", ...)),
        so two unrelated datastores sharing a moref across vCenters would have
        their used_pct samples interleaved into a single bogus trend line.
        """
        shared_moref = "datastore-1001"
        # vc-a: flat/no-growth series -> must NOT produce a forecast on its own.
        rows_a = _growing_series(moref=shared_moref, step=0.0, n=8, span_days=14)
        for r in rows_a:
            r.vcenter = "vc-a"
            r.name = "ds-a"
        # vc-b: clearly growing series -> must produce a forecast on its own.
        rows_b = _growing_series(moref=shared_moref, step=2.0, n=8, span_days=14)
        for r in rows_b:
            r.vcenter = "vc-b"
            r.name = "ds-b"

        agent = _agent(enabled=True)
        mock_session = _patch_session(rows_a + rows_b)

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_session
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = agent.collect()

        assert result == []
        added = mock_session.add.call_args_list
        # Only vc-b's growing series should promote an Instinct; if the two
        # vCenters' samples were wrongly merged by moref alone, the flat
        # vc-a samples interleaved with vc-b's growth would either suppress
        # the forecast entirely or produce a slope/pattern attributed to the
        # wrong (vcenter, moref) pair.
        assert len(added) == 1
        instinct = added[0][0][0]
        assert "vcenter=vc-b" in instinct.pattern
        assert "vc-b" in instinct.citation
        assert "vc-a" not in instinct.citation

    def test_too_few_samples_is_skipped(self):
        """Fewer than the minimum sample count must not produce a forecast."""
        agent = _agent(enabled=True)
        rows = _growing_series(n=3)
        mock_session = _patch_session(rows)

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_session
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = agent.collect()

        assert result == []
        assert mock_session.add.call_args_list == []


class TestEmptyResult:
    def test_no_metric_rows_returns_empty(self):
        """No VsphereDatastoreMetric history at all -> empty list, no writes."""
        agent = _agent(enabled=True)
        mock_session = _patch_session([])

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_session
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = agent.collect()

        assert result == []
        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_called()


class TestExceptionHandling:
    def test_collect_reraises_and_logs_on_db_error(self):
        """A DB error inside the session block must be logged and re-raised,
        never swallowed (same contract as DriftLearningAgent)."""
        agent = _agent(enabled=True)

        with patch("infra_brain.agents.capacity_forecast.get_session") as mock_ctx:
            mock_ctx.side_effect = RuntimeError("db unavailable")
            try:
                agent.collect()
                raised = False
            except RuntimeError:
                raised = True
        assert raised, "collect() must propagate DB errors, not swallow them"
