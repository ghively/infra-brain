"""Tests for IaC reader tools — Task 14."""

from unittest.mock import patch, MagicMock
from infra_brain.tools.iac_reader import (
    parse_ansible_inventory_content_tool,
    parse_terraform_resources_tool,
    find_iac_files_tool,
    read_iac_file_tool,
)


def test_parse_ansible_inventory_yaml():
    content = """
all:
  children:
    linux:
      hosts:
        server-01:
          ansible_host: 10.0.0.1
    windows:
      hosts:
        win-01:
          ansible_host: 10.0.0.2
"""
    result = parse_ansible_inventory_content_tool.invoke({"content": content})
    assert "linux" in result["groups"]
    assert "server-01" in result["hosts"]
    assert result["hosts"]["server-01"]["ansible_host"] == "10.0.0.1"


def test_parse_ansible_inventory_windows_host():
    content = """
all:
  children:
    windows:
      hosts:
        win-01:
          ansible_host: 10.0.0.2
"""
    result = parse_ansible_inventory_content_tool.invoke({"content": content})
    assert "windows" in result["groups"]
    assert "win-01" in result["hosts"]
    assert result["hosts"]["win-01"]["ansible_host"] == "10.0.0.2"


def test_parse_terraform_resources():
    content = """
resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.micro"
}

resource "aws_security_group" "sg" {
  name = "web-sg"
}
"""
    result = parse_terraform_resources_tool.invoke({"content": content})
    assert len(result) == 2
    assert result[0]["resource_type"] == "aws_instance"
    assert result[0]["resource_name"] == "web"
    assert result[1]["resource_type"] == "aws_security_group"


def test_parse_empty_content_returns_empty():
    result = parse_ansible_inventory_content_tool.invoke({"content": ""})
    assert result["groups"] == {}
    assert result["hosts"] == {}


def test_parse_terraform_empty_content():
    result = parse_terraform_resources_tool.invoke({"content": ""})
    assert result == []


def test_parse_terraform_no_resources():
    content = """
variable "region" {
  default = "us-east-1"
}
"""
    result = parse_terraform_resources_tool.invoke({"content": content})
    assert result == []


def test_parse_ansible_invalid_yaml():
    result = parse_ansible_inventory_content_tool.invoke({"content": ": bad: yaml: {"})
    assert "error" in result


def test_find_iac_files_tool_filters_extensions():
    tree = [
        {"path": "inventories/prod.yml", "name": "prod.yml", "type": "blob"},
        {"path": "playbooks/site.yaml", "name": "site.yaml", "type": "blob"},
        {"path": "terraform/main.tf", "name": "main.tf", "type": "blob"},
        {"path": "terraform/vars.tofu", "name": "vars.tofu", "type": "blob"},
        {"path": "README.md", "name": "README.md", "type": "blob"},
        {"path": "scripts/run.sh", "name": "run.sh", "type": "blob"},
        {"path": "inventories", "name": "inventories", "type": "tree"},
    ]
    with patch("infra_brain.tools.iac_reader.gitlab_repository_tree_tool") as mock_tree:
        mock_tree.invoke = MagicMock(return_value=tree)
        result = find_iac_files_tool.invoke({"project_id": 1, "ref": "main"})
    assert len(result) == 4
    paths = [r["path"] for r in result]
    assert "inventories/prod.yml" in paths
    assert "terraform/main.tf" in paths
    assert "README.md" not in paths
    assert "scripts/run.sh" not in paths


def test_read_iac_file_tool_delegates_to_gitlab():
    with patch("infra_brain.tools.iac_reader.gitlab_file_tool") as mock_file:
        mock_file.invoke = MagicMock(return_value="file content here")
        result = read_iac_file_tool.invoke(
            {"project_id": 1, "file_path": "inventories/prod.yml", "ref": "main"}
        )
    assert result == "file content here"
    mock_file.invoke.assert_called_once_with(
        {"project_id": 1, "file_path": "inventories/prod.yml", "ref": "main"}
    )


def test_parse_ansible_inventory_flat_groups():
    """Test inventory without 'all' wrapper — direct group names at top level."""
    content = """
webservers:
  hosts:
    web-01:
      ansible_host: 198.51.100.18
    web-02:
      ansible_host: 198.51.100.19
"""
    result = parse_ansible_inventory_content_tool.invoke({"content": content})
    assert "web-01" in result["hosts"]
    assert "web-02" in result["hosts"]
