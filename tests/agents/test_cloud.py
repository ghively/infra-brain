"""Tests for CloudAgent — tool-based AWS collection."""

from unittest.mock import patch

import pytest

from infra_brain.agents.base import CollectorSkipped
from infra_brain.agents.cloud import CloudAgent


def test_cloud_collect_raises_skipped_when_aws_disabled(make_agent):
    """collect() raises CollectorSkipped when aws_enabled is False."""
    with patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions:
        agent = make_agent(CloudAgent)
        agent.settings.aws_enabled = False
        with pytest.raises(CollectorSkipped):
            agent.collect()
    mock_regions.invoke.assert_not_called()


def test_cloud_collect_skipped_message_contains_reason(make_agent):
    """CollectorSkipped reason string mentions aws_enabled so the dashboard can display it."""
    agent = make_agent(CloudAgent)
    agent.settings.aws_enabled = False
    with pytest.raises(CollectorSkipped, match="aws_enabled"):
        agent.collect()


def test_cloud_collect_returns_empty_when_regions_empty(make_agent):
    """When no regions are returned, collect() returns []."""
    with (
        patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions,
        patch("infra_brain.agents.cloud.aws_ec2_instances_tool") as mock_instances,
    ):
        mock_regions.invoke.return_value = []
        agent = make_agent(CloudAgent)
        agent.settings.aws_enabled = True
        result = agent.collect()
    assert result.items == []
    mock_instances.invoke.assert_not_called()


def test_cloud_collect_calls_region_tools_when_enabled(make_agent):
    """collect() queries regions and per-region tools when aws_enabled=True."""
    with (
        patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions,
        patch("infra_brain.agents.cloud.aws_ec2_instances_tool") as mock_instances,
        patch("infra_brain.agents.cloud.aws_vpcs_tool") as mock_vpcs,
        patch("infra_brain.agents.cloud.aws_security_groups_tool") as mock_sgs,
    ):
        mock_regions.invoke.return_value = ["us-east-1"]
        mock_instances.invoke.return_value = []
        mock_vpcs.invoke.return_value = []
        mock_sgs.invoke.return_value = []
        agent = make_agent(CloudAgent)
        agent.settings.aws_enabled = True
        result = agent.collect()

    assert result.items == []
    mock_regions.invoke.assert_called_once()
    mock_instances.invoke.assert_called_once()


def test_settings_has_aws_enabled_default_false():
    """Settings.aws_enabled defaults to False."""
    from infra_brain.config import Settings

    s = Settings()
    assert s.aws_enabled is False
