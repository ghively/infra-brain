"""Tests that the 13 openui Custom-View components are wired to real data.

History: this file originally guarded the OPPOSITE contract — that the 13
non-FleetStatCard components rendered a visible "Preview — not yet wired to
data" placeholder via a shared ``PreviewStub`` component. That was correct
while those components really were unimplemented stubs.

**Deliberate test-contract change (2026-07-27, dashboard redesign Phase 0):**
the maintainer's explicit decision was to wire all 13 up to real backend data rather
than hide them — see docs/TRACKER.md TRK-199. ``PreviewStub`` has been
removed entirely from ``OpenUIRenderer.tsx``; every one of the 14 components
in ``COMPONENT_REGISTRY`` (at the time) was a real, independently-implemented
component that fetches live data via the same ``apiGet``/``AuthRequired``/
``Skeleton`` conventions the rest of dashboard-app uses (see that file's own
module docstring). This file now asserts THAT contract instead: no stub
delegation survives, every non-trivial component performs a real fetch, and
the full registry is still complete (the one assertion that didn't change).

**OctopusDeployHistory removed (2026-08-01, P7.1a)** — its backing route
(``/api/octopus/deployments/history``) was deleted along with the rest of the
Octopus dashboard surface (D6/D11); the registry now has 13 components.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
RENDERER_TSX = REPO_ROOT / "dashboard-app" / "src" / "openui" / "OpenUIRenderer.tsx"

# The 12 remaining components that were stubs before the 2026-07-27 wiring
# pass (OctopusDeployHistory, also formerly a stub, was removed in P7.1a).
FORMERLY_STUB_COMPONENTS = [
    "DriftEventTable",
    "SweepTimeline",
    "ResourceList",
    "VulnSummary",
    "ComplianceBoard",
    "AgentActivityFeed",
    "DomainHealthGrid",
    "LinuxPackageTable",
    "LinuxServiceStatus",
    "LinuxPortScan",
    "DriftTrendChart",
    "WindowsPatchState",
]

# FleetStatCard was always genuinely implemented — renders a value already
# embedded by the LLM in the tag, so it legitimately never fetches.
LIVE_COMPONENT = "FleetStatCard"

ALL_COMPONENTS = FORMERLY_STUB_COMPONENTS + [LIVE_COMPONENT]


def _source() -> str:
    assert RENDERER_TSX.exists(), f"OpenUIRenderer.tsx not found at {RENDERER_TSX}"
    return RENDERER_TSX.read_text(encoding="utf-8")


def _function_body(src: str, header: str) -> str:
    """Return the source slice of a function/const body via brace-counting.

    Robust against indentation changes, added helpers, or re-ordered
    definitions — unlike a fragile line-based sentinel.
    """
    start = src.find(header)
    assert start != -1, f"Could not locate {header!r} in OpenUIRenderer.tsx"
    if header.startswith("function "):
        anchor = src.find(")", start)  # close of the parameter list
    else:
        anchor = src.find("=", start)  # assignment operator
    assert anchor != -1, f"Could not find the body anchor for {header!r}"
    body_open = src.find("{", anchor)
    assert body_open != -1, f"Could not find the body opening brace for {header!r}"
    brace_depth = 0
    for i, ch in enumerate(src[body_open:], start=body_open):
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"Could not brace-count the end of {header!r} — check syntax")


def test_preview_stub_removed():
    """PreviewStub must no longer exist — every component is now real."""
    src = _source()
    assert "PreviewStub" not in src, (
        "PreviewStub still referenced in OpenUIRenderer.tsx — the 2026-07-27 "
        "wiring pass (TRK-199) removed it entirely. If a new stub-like "
        "placeholder was reintroduced, either wire the component to real data "
        "or update this test with a documented reason."
    )


def test_formerly_stub_components_fetch_real_data():
    """Each formerly-stub component must call the shared fetch hook.

    Every real component uses ``useOpenUIFetch`` (which wraps ``apiGet``
    internally) rather than fetching ad hoc — a component that renders
    static/preview content without ever fetching is exactly the regression
    TRK-199 fixed, so this guards against silently reverting to that state.
    """
    src = _source()
    assert "function useOpenUIFetch" in src, (
        "The shared useOpenUIFetch hook is missing entirely — every real "
        "component depends on it."
    )
    missing = []
    for name in FORMERLY_STUB_COMPONENTS:
        header = f"function {name}("
        if header not in src:
            missing.append(f"{name} (function not found)")
            continue
        body = _function_body(src, header)
        if "useOpenUIFetch" not in body:
            missing.append(f"{name} (no useOpenUIFetch call in its body)")
    assert not missing, (
        f"These components are not wired to real data: {missing}. "
        "Each must call useOpenUIFetch(...) to fetch live backend data."
    )


def test_fleet_stat_card_unchanged():
    """FleetStatCard was always real and must still not depend on a stub."""
    src = _source()
    body = _function_body(src, f"function {LIVE_COMPONENT}(")
    assert "PreviewStub" not in body, (
        f"{LIVE_COMPONENT} must not reference PreviewStub."
    )


def test_all_components_registered():
    """All 13 component names must appear in the COMPONENT_REGISTRY object."""
    src = _source()
    registry = _function_body(src, "const COMPONENT_REGISTRY")
    missing = [name for name in ALL_COMPONENTS if name not in registry]
    assert not missing, (
        f"Component names missing from COMPONENT_REGISTRY (registration broken): {missing}"
    )
