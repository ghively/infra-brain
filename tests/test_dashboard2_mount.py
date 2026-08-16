"""B1 (Wave 6.1 / DR-6.1): the Vite+React rebuild is served at /dashboard2 side-by-side
with the legacy /dashboard mount. This asserts the new mount is live and serves HTML."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infra_brain.main import create_app

# The built frontend bundle (dashboard-app's `npm run build` output) is not
# committed in this copy — see .gitignore — so a fresh checkout has nothing
# at this mount until it's built once. The tests that actually need bytes at
# /dashboard2/ skip gracefully rather than fail on an unbuilt tree; the two
# that only assert absence/404 behavior (missing_asset, path_traversal) do
# not depend on the bundle and always run.
_STATIC2_INDEX = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "infra_brain"
    / "dashboard"
    / "static2"
    / "index.html"
)
_needs_build = pytest.mark.skipif(
    not _STATIC2_INDEX.exists(),
    reason="dashboard-app is not built — run `cd dashboard-app && npm run build` first",
)


@_needs_build
def test_dashboard2_serves_html():
    client = TestClient(create_app())
    resp = client.get("/dashboard2/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@_needs_build
def test_dashboard2_deep_link_serves_spa_shell():
    """F13: BrowserRouter deep-links / reloads must serve index.html (SPA fallback)."""
    client = TestClient(create_app())
    resp = client.get("/dashboard2/vulns")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@_needs_build
def test_dashboard2_multi_segment_deep_link_serves_spa_shell():
    """F13: multi-segment router paths (e.g. /dashboard2/vulns/123/detail) also fall back."""
    client = TestClient(create_app())
    resp = client.get("/dashboard2/vulns/123/detail")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_dashboard2_missing_asset_still_404s():
    """F13: real (extensioned) asset paths that don't exist must NOT fall back to index.html."""
    client = TestClient(create_app())
    resp = client.get("/dashboard2/assets/missing-xyz.js")
    assert resp.status_code == 404


def test_dashboard2_path_traversal_does_not_escape_mount():
    """F13 defense-in-depth: traversal attempts must not serve files outside static2/."""
    client = TestClient(create_app())
    resp = client.get("/dashboard2/../../etc/passwd")
    # Either normalized away (404) or blocked — must never 200 with non-HTML file content.
    assert resp.status_code in (400, 404) or resp.headers["content-type"].startswith("text/html")
