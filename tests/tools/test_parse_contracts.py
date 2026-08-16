"""Un-mocked parse / pagination contract tests.

Unlike the rest of the suite (which mocks tool functions wholesale), these feed
real-shaped payloads through the ACTUAL parsing/pagination code with only the
network boundary stubbed. They would catch a regression in pagination logic,
base64 decoding, the Octopus Items/TotalResults envelope, or HCL/YAML parsing —
none of which the mocked tests exercise.
"""

import base64
from types import SimpleNamespace

import httpx

import infra_brain.tools.gitlab as gitlab_mod
import infra_brain.tools.octopus_tool as octopus_mod
import infra_brain.tools.rapid7 as rapid7_mod
from infra_brain.tools.iac_reader import (
    parse_ansible_inventory_content_tool,
    parse_terraform_resources_tool,
)


def _gitlab_settings():
    return SimpleNamespace(
        gitlab_url="https://gitlab.example.com",
        gitlab_token="tok",
        gitlab_ssl_verify=True,
        api_timeout_seconds=5,
        api_page_size=100,
    )


# --------------------------------------------------------------------------- #
# GitLab — X-Next-Page pagination walks every page
# --------------------------------------------------------------------------- #
def test_gitlab_pagination_walks_all_pages(monkeypatch):
    bodies = {
        1: ([{"id": 1}, {"id": 2}], {"X-Next-Page": "2"}),
        2: ([{"id": 3}], {"X-Next-Page": ""}),
    }
    seen_params = []

    def fake_get(url, **kwargs):
        # page travels via the params dict (httpx replaces the URL query when params
        # is passed), so read it from there — not from the URL string.
        params = kwargs.get("params") or {}
        seen_params.append(params)
        page = int(params["page"])
        body, headers = bodies[page]
        return httpx.Response(200, json=body, headers=headers, request=httpx.Request("GET", url))

    monkeypatch.setattr(gitlab_mod, "get_settings", _gitlab_settings)
    monkeypatch.setattr(gitlab_mod, "readonly_get", fake_get)

    result = gitlab_mod.gitlab_get_paginated("/api/v4/projects?membership=true")
    assert [r["id"] for r in result] == [1, 2, 3]  # all pages concatenated, in order
    # embedded query (?membership=true) must survive alongside pagination params,
    # since httpx replaces the URL query whenever a params dict is supplied.
    assert all(p.get("membership") == "true" for p in seen_params)
    assert all(p.get("per_page") == 100 for p in seen_params)


# --------------------------------------------------------------------------- #
# GitLab — base64 file content is actually decoded
# --------------------------------------------------------------------------- #
def test_gitlab_file_tool_decodes_base64(monkeypatch):
    raw = "hostname: core-sw-01\nrole: switch\n"
    encoded = base64.b64encode(raw.encode()).decode()

    def fake_get(url, **kwargs):
        return httpx.Response(
            200,
            json={"encoding": "base64", "content": encoded},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(gitlab_mod, "get_settings", _gitlab_settings)
    monkeypatch.setattr(gitlab_mod, "readonly_get", fake_get)

    out = gitlab_mod.gitlab_file_tool.invoke(
        {"project_id": 1, "file_path": "inv/host.yml", "ref": "main"}
    )
    assert out == raw  # real base64 decode, not the raw encoded blob


# --------------------------------------------------------------------------- #
# Rapid7 — page.totalPages pagination concatenates resources
# --------------------------------------------------------------------------- #
def test_rapid7_pagination_concatenates_resources(monkeypatch):
    payloads = {
        0: {"resources": [{"id": 10}, {"id": 11}], "page": {"totalPages": 2}},
        1: {"resources": [{"id": 12}], "page": {"totalPages": 2}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page"))
        return httpx.Response(200, json=payloads[page])

    def fake_client(**kwargs):
        return httpx.Client(
            base_url="https://r7.example.com", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(rapid7_mod, "ReadOnlyClient", fake_client)
    monkeypatch.setattr(
        rapid7_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rapid7_url="https://r7.example.com",
            rapid7_username="user",
            rapid7_api_key="key",
            rapid7_ssl_verify=True,
            api_timeout_seconds=30,
        ),
    )

    result = rapid7_mod._get_all_pages("/api/3/assets", page_size=2)
    assert [r["id"] for r in result] == [10, 11, 12]


# --------------------------------------------------------------------------- #
# Octopus — Items/TotalResults envelope pagination
# --------------------------------------------------------------------------- #
def test_octopus_envelope_pagination(monkeypatch):
    def fake_settings():
        return SimpleNamespace(
            octopus_url="https://octo.example.com",
            octopus_api_key="key",
            octopus_ssl_verify=True,
            api_timeout_seconds=5,
            api_page_size=2,
        )

    def fake_get(url, **kwargs):
        req = httpx.Request("GET", url)
        skip = int(url.split("skip=")[1].split("&")[0])
        if skip == 0:
            return httpx.Response(
                200, json={"Items": [{"Name": "a"}, {"Name": "b"}], "TotalResults": 3}, request=req
            )
        return httpx.Response(200, json={"Items": [{"Name": "c"}], "TotalResults": 3}, request=req)

    monkeypatch.setattr(octopus_mod, "_settings", fake_settings)
    monkeypatch.setattr(octopus_mod, "readonly_get", fake_get)

    result = octopus_mod.octopus_get_paginated("/api/projects")
    assert [r["Name"] for r in result] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# IaC — real HCL / YAML parsing (pure functions)
# --------------------------------------------------------------------------- #
def test_parse_terraform_resources_extracts_blocks():
    hcl = """
    resource "aws_instance" "web" {
      ami = "ami-123"
    }
    resource "aws_s3_bucket" "data" {
      bucket = "my-bucket"
    }
    """
    result = parse_terraform_resources_tool.invoke({"content": hcl})
    assert {(r["resource_type"], r["resource_name"]) for r in result} == {
        ("aws_instance", "web"),
        ("aws_s3_bucket", "data"),
    }


def test_parse_ansible_inventory_walks_groups_and_hosts():
    inv = """
    all:
      children:
        webservers:
          hosts:
            web01:
              ansible_host: 10.0.0.1
            web02: {}
    """
    result = parse_ansible_inventory_content_tool.invoke({"content": inv})
    assert result["groups"]["webservers"] == ["web01", "web02"]
    assert result["hosts"]["web01"]["ansible_host"] == "10.0.0.1"


def test_parse_ansible_inventory_handles_invalid_yaml():
    result = parse_ansible_inventory_content_tool.invoke({"content": "foo: [unbalanced"})
    assert result == {"groups": {}, "hosts": {}, "error": "invalid YAML"}
