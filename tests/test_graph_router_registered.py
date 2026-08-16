"""Smoke test: graph_router is wired into the FastAPI app via create_app()."""

from unittest.mock import MagicMock, patch


def test_graph_routes_appear_in_app():
    """After create_app(), /api/graph/* routes must exist."""
    with (
        patch("infra_brain.main.load_secrets_into_env"),
        patch("infra_brain.main.init_tracing"),
        patch("infra_brain.main.check_freshness", return_value=[]),
        patch("infra_brain.main.SchedulerService") as mock_sched,
    ):
        mock_sched.return_value.start = MagicMock()
        mock_sched.return_value.stop = MagicMock()
        from infra_brain.main import create_app

        app = create_app()

    # Use the OpenAPI schema to find registered routes (include_router adds
    # _IncludedRouter objects that don't expose .path directly in app.routes).
    schema = app.openapi()
    paths = set(schema.get("paths", {}).keys())
    graph_paths = {p for p in paths if p.startswith("/api/graph")}
    assert len(graph_paths) >= 4, f"Expected >=4 graph routes, got: {graph_paths}"
