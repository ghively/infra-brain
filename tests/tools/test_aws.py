"""Tests for infra_brain.tools.aws — read-only boto3-backed EC2 collection.

Module tests (MR-E TEST-4): this module had zero test coverage before.
boto3 is optional (see ``_boto3()``'s ImportError guard) so every test patches
``infra_brain.tools.aws._boto3`` directly rather than depending on the real
package being installed.
"""

from unittest.mock import MagicMock, patch

from infra_brain.tools import aws as aws_mod
from infra_brain.tools.aws import (
    aws_ec2_instances_tool,
    aws_ec2_regions_tool,
    aws_security_groups_tool,
    aws_vpcs_tool,
)


def _patch_boto3(fake_module):
    return patch.object(aws_mod, "_boto3", return_value=fake_module)


# ---------------------------------------------------------------------------
# boto3 unavailable -> every tool degrades to an empty list, never raises
# ---------------------------------------------------------------------------


def test_ec2_regions_tool_returns_empty_when_boto3_unavailable():
    with _patch_boto3(None):
        assert aws_ec2_regions_tool.invoke({}) == []


def test_ec2_instances_tool_returns_empty_when_boto3_unavailable():
    with _patch_boto3(None):
        assert aws_ec2_instances_tool.invoke({"region": "us-east-1"}) == []


def test_vpcs_tool_returns_empty_when_boto3_unavailable():
    with _patch_boto3(None):
        assert aws_vpcs_tool.invoke({"region": "us-east-1"}) == []


def test_security_groups_tool_returns_empty_when_boto3_unavailable():
    with _patch_boto3(None):
        assert aws_security_groups_tool.invoke({"region": "us-east-1"}) == []


# ---------------------------------------------------------------------------
# aws_ec2_regions_tool
# ---------------------------------------------------------------------------


def test_ec2_regions_tool_returns_region_names():
    fake_ec2 = MagicMock()
    fake_ec2.describe_regions.return_value = {
        "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"}]
    }
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_ec2_regions_tool.invoke({})

    assert result == ["us-east-1", "eu-west-1"]
    fake_ec2.describe_regions.assert_called_once_with(AllRegions=False)


def test_ec2_regions_tool_empty_response():
    fake_ec2 = MagicMock()
    fake_ec2.describe_regions.return_value = {}
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        assert aws_ec2_regions_tool.invoke({}) == []


# ---------------------------------------------------------------------------
# aws_ec2_instances_tool
# ---------------------------------------------------------------------------


def _paginated(pages):
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    return paginator


def test_ec2_instances_tool_uses_name_tag_when_present():
    pages = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-abc123",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running"},
                            "PrivateIpAddress": "10.0.0.5",
                            "PublicIpAddress": "1.2.3.4",
                            "Platform": "linux",
                            "Tags": [{"Key": "Name", "Value": "web-01"}],
                        }
                    ]
                }
            ]
        }
    ]
    fake_ec2 = MagicMock()
    fake_ec2.get_paginator.return_value = _paginated(pages)
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_ec2_instances_tool.invoke({"region": "us-east-1"})

    assert len(result) == 1
    item = result[0]
    assert item["name"] == "web-01"
    assert item["type"] == "ec2_instance"
    assert item["data"]["instance_id"] == "i-abc123"
    assert item["data"]["instance_type"] == "t3.micro"
    assert item["data"]["state"] == "running"
    assert item["data"]["region"] == "us-east-1"
    assert item["data"]["private_ip"] == "10.0.0.5"
    assert item["data"]["public_ip"] == "1.2.3.4"
    fake_ec2.get_paginator.assert_called_once_with("describe_instances")


def test_ec2_instances_tool_falls_back_to_instance_id_when_no_name_tag():
    pages = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-noname",
                            "Tags": [{"Key": "Environment", "Value": "prod"}],
                        }
                    ]
                }
            ]
        }
    ]
    fake_ec2 = MagicMock()
    fake_ec2.get_paginator.return_value = _paginated(pages)
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_ec2_instances_tool.invoke({"region": "us-west-2"})

    assert result[0]["name"] == "i-noname"
    # Defaults for absent optional fields.
    assert result[0]["data"]["instance_type"] == ""
    assert result[0]["data"]["state"] == ""
    assert result[0]["data"]["platform"] == "linux"


def test_ec2_instances_tool_no_reservations_returns_empty():
    fake_ec2 = MagicMock()
    fake_ec2.get_paginator.return_value = _paginated([{}])
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        assert aws_ec2_instances_tool.invoke({"region": "us-east-1"}) == []


def test_ec2_instances_tool_multiple_instances_across_pages():
    pages = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "Tags": []}]}]},
        {"Reservations": [{"Instances": [{"InstanceId": "i-2", "Tags": []}]}]},
    ]
    fake_ec2 = MagicMock()
    fake_ec2.get_paginator.return_value = _paginated(pages)
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_ec2_instances_tool.invoke({"region": "us-east-1"})

    assert {r["data"]["instance_id"] for r in result} == {"i-1", "i-2"}


# ---------------------------------------------------------------------------
# aws_vpcs_tool
# ---------------------------------------------------------------------------


def test_vpcs_tool_uses_name_tag_when_present():
    fake_ec2 = MagicMock()
    fake_ec2.describe_vpcs.return_value = {
        "Vpcs": [
            {
                "VpcId": "vpc-123",
                "CidrBlock": "10.0.0.0/16",
                "IsDefault": False,
                "Tags": [{"Key": "Name", "Value": "prod-vpc"}],
            }
        ]
    }
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_vpcs_tool.invoke({"region": "us-east-1"})

    assert len(result) == 1
    assert result[0]["name"] == "prod-vpc"
    assert result[0]["type"] == "vpc"
    assert result[0]["data"]["vpc_id"] == "vpc-123"
    assert result[0]["data"]["cidr_block"] == "10.0.0.0/16"
    assert result[0]["data"]["is_default"] is False


def test_vpcs_tool_falls_back_to_vpc_id_when_no_name_tag():
    fake_ec2 = MagicMock()
    fake_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-999"}]}
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_vpcs_tool.invoke({"region": "us-east-1"})

    assert result[0]["name"] == "vpc-999"
    assert result[0]["data"]["cidr_block"] == ""
    assert result[0]["data"]["is_default"] is False


def test_vpcs_tool_empty_response():
    fake_ec2 = MagicMock()
    fake_ec2.describe_vpcs.return_value = {}
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        assert aws_vpcs_tool.invoke({"region": "us-east-1"}) == []


# ---------------------------------------------------------------------------
# aws_security_groups_tool
# ---------------------------------------------------------------------------


def test_security_groups_tool_uses_group_name_when_present():
    fake_ec2 = MagicMock()
    fake_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [
            {
                "GroupId": "sg-1",
                "GroupName": "web-sg",
                "Description": "allows 443",
                "VpcId": "vpc-1",
            }
        ]
    }
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_security_groups_tool.invoke({"region": "us-east-1"})

    assert len(result) == 1
    assert result[0]["name"] == "web-sg"
    assert result[0]["type"] == "security_group"
    assert result[0]["data"]["group_id"] == "sg-1"
    assert result[0]["data"]["description"] == "allows 443"
    assert result[0]["data"]["vpc_id"] == "vpc-1"


def test_security_groups_tool_falls_back_to_group_id_when_no_name():
    fake_ec2 = MagicMock()
    fake_ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-anon"}]}
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        result = aws_security_groups_tool.invoke({"region": "us-east-1"})

    assert result[0]["name"] == "sg-anon"
    assert result[0]["data"]["description"] == ""


def test_security_groups_tool_empty_response():
    fake_ec2 = MagicMock()
    fake_ec2.describe_security_groups.return_value = {}
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        assert aws_security_groups_tool.invoke({"region": "us-east-1"}) == []


# ---------------------------------------------------------------------------
# Regional client wiring — every regional tool must scope the client to the
# requested region, not a default/global client.
# ---------------------------------------------------------------------------


def test_ec2_instances_tool_scopes_client_to_requested_region():
    fake_ec2 = MagicMock()
    fake_ec2.get_paginator.return_value = _paginated([{}])
    fake_b3 = MagicMock()
    fake_b3.client.return_value = fake_ec2

    with _patch_boto3(fake_b3):
        aws_ec2_instances_tool.invoke({"region": "ap-southeast-2"})

    fake_b3.client.assert_called_once_with("ec2", region_name="ap-southeast-2")
