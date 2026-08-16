"""F-020/F-039 contract gate: every JSON API route must declare a TYPED
response_model, and never the shapeless `dict`.

The old contract-check only diffed generated-from-schemas artifacts against
themselves, so a route with no response_model (or envelope-vs-array drift like
F-024) sailed through. This test walks the real app's route table instead.
"""

from fastapi.routing import APIRoute

from infra_brain.main import create_app

# (method, path) pairs that legitimately carry no response_model, with reasons.
ALLOWLIST = {
    ("GET", "/"),  # 302 redirect to /dashboard
    ("POST", "/api/dashboard/chat"),  # SSE StreamingResponse (text/event-stream)
    ("POST", "/api/dashboard/custom-view"),  # NDJSON StreamingResponse
    ("DELETE", "/api/dashboard/views/{view_id}"),  # 204 No Content
    ("DELETE", "/api/dashboard/runtime-config/{key}"),  # 204 No Content (TRK-303)
    ("GET", "/metrics"),  # Prometheus text exposition format, not JSON (TRK-093)
    # GraphQL's response shape is query-defined (GitLab #114) — Strawberry's
    # GraphQLRouter has no fixed pydantic response_model to declare, by design.
    ("GET", "/api/graphql"),
    ("POST", "/api/graphql"),
    # GitLab #111 chatops bot: each route returns a JSONResponse directly (Slack's
    # url_verification challenge echo, Slack Events API ack, Slack slash-command
    # ack, or a Teams bot-reply body) — response_model has no effect on a handler
    # that returns a Response object directly, and the reply shapes are dictated
    # by Slack's/Teams' own wire formats, not an internal schema worth typing.
    ("POST", "/webhooks/chatops/slack/events"),
    ("POST", "/webhooks/chatops/slack/command"),
    ("POST", "/webhooks/chatops/teams"),
}


def _iter_api_routes(routes):
    """Yield every APIRoute reachable from ``routes``, recursively.

    FastAPI 0.137.x uses *lazy* router inclusion: ``app.include_router()`` inserts
    a ``_IncludedRouter`` placeholder into ``app.routes`` instead of eagerly
    flattening the sub-router's APIRoutes onto the app. Walking ``app.routes`` for
    ``APIRoute`` instances alone would therefore only ever see routes decorated
    directly on the app object (here just ``GET /``) — making this contract gate
    vacuous. Descend into each placeholder's ``original_router`` so the gate
    inspects the real, fully-resolved route table (all 70+ routes).
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            sub = getattr(route, "original_router", None)
            if sub is not None and hasattr(sub, "routes"):
                yield from _iter_api_routes(sub.routes)


def _api_routes():
    app = create_app()
    for route in _iter_api_routes(app.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, route


def test_every_api_route_declares_response_model():
    missing = []
    for method, route in _api_routes():
        if (method, route.path) in ALLOWLIST:
            continue
        if route.response_model is None:
            missing.append(f"{method} {route.path}")
    assert missing == [], f"routes missing response_model: {missing}"


def test_no_route_uses_shapeless_dict_response_model():
    shapeless = []
    for method, route in _api_routes():
        if route.response_model is dict:
            shapeless.append(f"{method} {route.path}")
    assert shapeless == [], f"routes with shapeless response_model=dict: {shapeless}"
