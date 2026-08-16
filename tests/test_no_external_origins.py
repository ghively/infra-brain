"""Tests for scripts/design_sync/check_no_external_origins.py (Wave 6.1 B7).

Wave 6.1 B7 (DR-6.1): moved from tests/test_vendored_assets.py (deleted with the
legacy DC-framework assets). The external-origins scanner is retained; it now covers
the Vite+React dashboard-app build (static2/) instead of the legacy static tree.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()


def _load_check_module():
    """Load scripts/design_sync/check_no_external_origins without requiring it
    to be on sys.path (the scripts/ package is not installed via pyproject.toml).
    """
    mod_path = REPO_ROOT / "scripts" / "design_sync" / "check_no_external_origins.py"
    assert mod_path.exists(), f"check_no_external_origins.py not found at {mod_path}"
    spec = importlib.util.spec_from_file_location("check_no_external_origins", mod_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_banned_origins_list_is_comprehensive():
    """BANNED_ORIGINS must cover all required CDN origins."""
    mod = _load_check_module()
    required = {
        "unpkg.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "jsdelivr.net",
        "cdnjs.cloudflare.com",
    }
    for origin in required:
        assert any(origin in b or b in origin for b in mod.BANNED_ORIGINS), (
            f"BANNED_ORIGINS does not cover required origin '{origin}'. "
            f"Current list: {mod.BANNED_ORIGINS}"
        )


def test_check_passes_on_real_tree():
    """check() must return no hits on the current (clean) repository tree."""
    mod = _load_check_module()
    hits = mod.check()
    assert hits == [], (
        "check_no_external_origins found violations in the real tree — "
        "a CDN origin has been introduced into a served asset or source file:\n"
        + "\n".join(f"  [{origin}] {path}  {loc}" for path, loc, origin in hits)
    )


def test_check_detects_injected_unpkg(tmp_path):
    """check() must flag unpkg.com injected into a scanned source file."""
    mod = _load_check_module()
    # Simulate a dashboard-app/src directory with a banned origin
    src = tmp_path / "dashboard-app" / "src"
    src.mkdir(parents=True)
    (src / "Bad.tsx").write_text(
        'const url = "https://unpkg.com/react@18/umd/react.production.min.js";',
        encoding="utf-8",
    )
    hits = mod.check(repo_root=tmp_path)
    assert len(hits) >= 1, "Expected at least one hit for injected unpkg.com origin"
    assert any(h[2] == "unpkg.com" for h in hits), (
        f"Expected 'unpkg.com' in hit origins, got: {[h[2] for h in hits]}"
    )


def test_check_detects_injected_googleapis_in_build(tmp_path):
    """check() must flag fonts.googleapis.com injected into the static2 build output."""
    mod = _load_check_module()
    static2 = tmp_path / "src" / "infra_brain" / "dashboard" / "static2"
    static2.mkdir(parents=True)
    (static2 / "index.html").write_text(
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        encoding="utf-8",
    )
    hits = mod.check(repo_root=tmp_path)
    assert len(hits) >= 1, "Expected at least one hit for injected fonts.googleapis.com"
    assert any("googleapis" in h[2] for h in hits), (
        f"Expected googleapis origin in hits, got: {[h[2] for h in hits]}"
    )


def test_scanned_files_includes_static2_and_source(tmp_path):
    """_scanned_files() must include both the Vite build output and the TypeScript sources."""
    mod = _load_check_module()
    # Create the expected paths
    static2 = tmp_path / "src" / "infra_brain" / "dashboard" / "static2"
    static2.mkdir(parents=True)
    index = static2 / "index.html"
    index.write_text("<html></html>")
    assets = static2 / "assets"
    assets.mkdir()
    js_chunk = assets / "index-abc123.js"
    js_chunk.write_text("var x = 1;")

    src = tmp_path / "dashboard-app" / "src"
    src.mkdir(parents=True)
    tsx = src / "App.tsx"
    tsx.write_text("export default function App() { return null; }")

    files = mod._scanned_files(repo_root=tmp_path)
    paths = [str(f) for f in files]
    assert str(index) in paths, "static2/index.html must be scanned"
    assert str(js_chunk) in paths, "static2/assets/*.js must be scanned"
    assert str(tsx) in paths, "dashboard-app/src/*.tsx must be scanned"
