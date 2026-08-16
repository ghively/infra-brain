from unittest.mock import patch, MagicMock
import base64
from infra_brain.tools.gitlab import (
    gitlab_get_paginated,
    gitlab_file_tool,
    gitlab_repository_tree_tool,
    gitlab_schedules_tool,
    gitlab_branches_tool,
    gitlab_runners_tool,
)
from infra_brain.tools.gitlab import gitlab_pipelines_tool as _gitlab_pipelines_tool
import httpx as _httpx_g
import pytest as _pytest_g
from langchain_core.tools import ToolException as _ToolException_g


def _mock_response(data, next_page=None):
    resp = MagicMock()
    resp.json.return_value = data
    resp.headers = {"X-Next-Page": str(next_page) if next_page else ""}
    resp.raise_for_status = MagicMock()
    return resp


def test_gitlab_get_paginated_follows_pages():
    with patch("infra_brain.tools.gitlab.readonly_get") as mock_get:
        mock_get.side_effect = [
            _mock_response([{"id": 1}], next_page=2),
            _mock_response([{"id": 2}], next_page=None),
        ]
        result = gitlab_get_paginated("/api/v4/projects")
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[1]["id"] == 2


def test_gitlab_get_paginated_single_page():
    with patch("infra_brain.tools.gitlab.readonly_get") as mock_get:
        mock_get.side_effect = [
            _mock_response([{"id": 10}, {"id": 20}], next_page=None),
        ]
        result = gitlab_get_paginated("/api/v4/runners/all")
    assert len(result) == 2
    assert mock_get.call_count == 1


def test_gitlab_file_tool_decodes_content():
    content = "hello world"
    encoded = base64.b64encode(content.encode()).decode()
    with patch(
        "infra_brain.tools.gitlab.gitlab_get",
        return_value={"content": encoded, "encoding": "base64"},
    ):
        result = gitlab_file_tool.invoke({"project_id": 1, "file_path": "README.md", "ref": "main"})
    assert result == content


def test_gitlab_file_tool_plain_content():
    with patch(
        "infra_brain.tools.gitlab.gitlab_get",
        return_value={"content": "plain text", "encoding": "text"},
    ):
        result = gitlab_file_tool.invoke({"project_id": 1, "file_path": "README.md", "ref": "main"})
    assert result == "plain text"


def test_gitlab_repository_tree_tool_returns_list():
    tree = [{"id": "abc", "name": "file.py", "type": "blob"}]
    with patch("infra_brain.tools.gitlab.gitlab_get_paginated", return_value=tree):
        result = gitlab_repository_tree_tool.invoke(
            {"project_id": 3, "path": "", "ref": "main", "recursive": False}
        )
    assert result[0]["name"] == "file.py"


def test_gitlab_repository_tree_tool_encodes_special_chars():
    """A path/ref containing '#' or '&' must be percent-encoded before being
    interpolated into the query string — otherwise '#' truncates the request
    at a URL fragment and '&' injects a bogus extra query parameter, silently
    dropping the rest of the path and the ref/recursive params."""
    with patch("infra_brain.tools.gitlab.gitlab_get_paginated", return_value=[]) as mock_paginated:
        gitlab_repository_tree_tool.invoke(
            {"project_id": 3, "path": "docs/a#b&c", "ref": "feature/x#1", "recursive": True}
        )
    called_path = mock_paginated.call_args[0][0]
    assert "#" not in called_path.split("?", 1)[1]
    assert called_path.count("&") == 2  # only the &ref= and &recursive=true separators
    assert "path=docs%2Fa%23b%26c" in called_path
    assert "ref=feature%2Fx%231" in called_path
    assert "&recursive=true" in called_path


def test_gitlab_schedules_tool_returns_list():
    with patch(
        "infra_brain.tools.gitlab.gitlab_get_paginated",
        return_value=[{"id": 1, "description": "weekly"}],
    ):
        result = gitlab_schedules_tool.invoke({"project_id": 5})
    assert result[0]["description"] == "weekly"


def test_gitlab_branches_tool_returns_list():
    branches = [{"name": "main"}, {"name": "feat/x"}]
    with patch("infra_brain.tools.gitlab.gitlab_get_paginated", return_value=branches):
        result = gitlab_branches_tool.invoke({"project_id": 7})
    assert len(result) == 2
    assert result[0]["name"] == "main"


# ---------------------------------------------------------------------------
# TRK-024 — error path raises ToolException (consistency with @tool_http_errors)
# ---------------------------------------------------------------------------
def _gitlab_settings():
    s = MagicMock()
    s.gitlab_url = "https://gitlab.example.com"
    s.gitlab_token = "tok"
    s.gitlab_ssl_verify = True
    s.api_timeout_seconds = 30
    s.api_page_size = 50
    return s


def _gitlab_status_error_resp(code=404):
    req = _httpx_g.Request("GET", "https://gitlab.example.com/x")
    resp = _httpx_g.Response(code, request=req)
    r = MagicMock()
    r.raise_for_status.side_effect = _httpx_g.HTTPStatusError(str(code), request=req, response=resp)
    return r


def test_gitlab_http_error_raises_toolexception():
    """TRK-024: an httpx HTTP error surfaces as ToolException via @tool_http_errors,
    consistent with the other HTTP-backed tool modules. 404 is non-transient so
    tenacity does not retry it."""
    with (
        patch("infra_brain.tools.gitlab.get_settings", return_value=_gitlab_settings()),
        patch(
            "infra_brain.tools.gitlab.readonly_get",
            return_value=_gitlab_status_error_resp(404),
        ),
    ):
        with _pytest_g.raises(_ToolException_g):
            _gitlab_pipelines_tool.invoke({"project_id": 1})


def test_gitlab_runners_tool_returns_list():
    runners = [{"id": 1, "description": "shared-runner-1"}, {"id": 2, "description": "shared-runner-2"}]
    with patch("infra_brain.tools.gitlab.gitlab_get", return_value=runners):
        result = gitlab_runners_tool.invoke({})
    assert len(result) == 2
    assert result[0]["description"] == "shared-runner-1"
