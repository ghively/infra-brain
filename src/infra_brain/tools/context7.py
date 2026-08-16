"""Context7 documentation/best-practice lookups — read-only by construction."""

from langchain_core.tools import tool

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import readonly_get
from infra_brain.tools.http_readonly import tool_http_errors

_BASE = "https://context7.com/api/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().context7_api_key}"}


@tool
@tool_http_errors
def context7_resolve_library_tool(name: str) -> dict:
    """Resolve a library/tool name to a Context7 library ID (read-only)."""
    if not get_settings().context7_api_key:
        return {"error": "context7 not configured"}
    resp = readonly_get(
        f"{_BASE}/search",
        headers=_headers(),
        params={"query": name},
        timeout=get_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


@tool
@tool_http_errors
def context7_docs_tool(library_id: str, topic: str = "") -> str:
    """Fetch best-practice documentation for a library/topic from Context7 (read-only)."""
    if not get_settings().context7_api_key:
        return "context7 not configured"
    resp = readonly_get(
        f"{_BASE}{library_id}",
        headers=_headers(),
        params={"topic": topic} if topic else {},
        timeout=get_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("content", "") if isinstance(data, dict) else str(data)
