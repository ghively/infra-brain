"""AWS read-only tools — boto3-backed EC2 resource collection."""

import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _boto3():
    try:
        import boto3 as _b3

        return _b3
    except ImportError:
        return None


@tool
def aws_ec2_regions_tool() -> list:
    """List active AWS EC2 regions (read-only)."""
    b3 = _boto3()
    if b3 is None:
        return []
    ec2 = b3.client("ec2")
    resp = ec2.describe_regions(AllRegions=False)
    return [r["RegionName"] for r in resp.get("Regions", [])]


@tool
def aws_ec2_instances_tool(region: str) -> list:
    """List EC2 instances in a region with name, type, state, and IPs (read-only)."""
    b3 = _boto3()
    if b3 is None:
        return []
    regional_ec2 = b3.client("ec2", region_name=region)
    items = []
    paginator = regional_ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                name = next(
                    (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                    instance["InstanceId"],
                )
                items.append(
                    {
                        "name": name,
                        "type": "ec2_instance",
                        "data": {
                            "instance_id": instance["InstanceId"],
                            "instance_type": instance.get("InstanceType", ""),
                            "state": instance.get("State", {}).get("Name", ""),
                            "region": region,
                            "private_ip": instance.get("PrivateIpAddress", ""),
                            "public_ip": instance.get("PublicIpAddress", ""),
                            "platform": instance.get("Platform", "linux"),
                        },
                    }
                )
    return items


@tool
def aws_vpcs_tool(region: str) -> list:
    """List VPCs in a region (read-only)."""
    b3 = _boto3()
    if b3 is None:
        return []
    regional_ec2 = b3.client("ec2", region_name=region)
    items = []
    for vpc in regional_ec2.describe_vpcs().get("Vpcs", []):
        vpc_name = next(
            (t["Value"] for t in vpc.get("Tags", []) if t["Key"] == "Name"),
            vpc["VpcId"],
        )
        items.append(
            {
                "name": vpc_name,
                "type": "vpc",
                "data": {
                    "vpc_id": vpc["VpcId"],
                    "cidr_block": vpc.get("CidrBlock", ""),
                    "is_default": vpc.get("IsDefault", False),
                    "region": region,
                },
            }
        )
    return items


@tool
def aws_security_groups_tool(region: str) -> list:
    """List security groups in a region (read-only)."""
    b3 = _boto3()
    if b3 is None:
        return []
    regional_ec2 = b3.client("ec2", region_name=region)
    items = []
    for sg in regional_ec2.describe_security_groups().get("SecurityGroups", []):
        items.append(
            {
                "name": sg.get("GroupName", sg["GroupId"]),
                "type": "security_group",
                "data": {
                    "group_id": sg["GroupId"],
                    "description": sg.get("Description", ""),
                    "vpc_id": sg.get("VpcId", ""),
                    "region": region,
                },
            }
        )
    return items
