"""CI guard: fail if any external CDN origin appears in the served frontend assets.

This module is the single source of truth for:
  - the BANNED_ORIGINS list
  - the file paths that are scanned (served HTML/CSS/JS sources)

Wave 6.1 B7 (DR-6.1): the legacy DC-framework assets (index.html, support.js,
vendor/ blobs, dashboard/src/*.dc.html) were deleted when /dashboard was retired.
The scanner now covers the Vite+React dashboard served at /dashboard2 (static2/).

It is imported by both:
  - the .gitlab-ci.yml ``no-external-origins`` preflight job
  - tests/test_no_external_origins.py

Rationale
---------
The dashboard-app Vite build should contain no external CDN origin references.
All fonts, icons, and JS are bundled into the build artifact at build time via
npm (no hand-vendored blobs, no runtime CDN fetches).

  Scanned
  ~~~~~~~
  - src/infra_brain/dashboard/static2/index.html   (Vite build entry — what the browser loads)
  - src/infra_brain/dashboard/static2/assets/*.js  (Vite-chunked JS bundles)
  - src/infra_brain/dashboard/static2/assets/*.css (Vite-chunked CSS bundles)
  - dashboard-app/src/**/*.tsx                     (source — catches regressions before build)
  - dashboard-app/src/**/*.ts                      (source)
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — single source of truth
# ---------------------------------------------------------------------------

#: Origins that must NEVER appear in any served (non-blob) frontend asset.
BANNED_ORIGINS: list[str] = [
    "unpkg.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
    "jsdelivr.net",
    "cdnjs.cloudflare.com",
    "cdnjs.com",
]

# Repo root is three levels above this file:
#   scripts/design_sync/check_no_external_origins.py  ->  scripts/design_sync/  ->  scripts/  ->  repo_root
_MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = _MODULE_DIR.parents[1]


def _scanned_files(repo_root: Path | None = None) -> list[Path]:
    """Return the list of files that the check scans.

    Accepting an optional *repo_root* override makes the function fully testable
    against a temp directory without touching the real checkout.
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    static2 = root / "src" / "infra_brain" / "dashboard" / "static2"
    dashboard_app_src = root / "dashboard-app" / "src"

    candidates: list[Path] = []

    # Vite build entry point
    index = static2 / "index.html"
    if index.exists():
        candidates.append(index)

    # Vite-chunked JS and CSS bundles
    assets_dir = static2 / "assets"
    if assets_dir.is_dir():
        candidates.extend(sorted(assets_dir.glob("*.js")))
        candidates.extend(sorted(assets_dir.glob("*.css")))

    # React/TypeScript source (catches regressions before build)
    if dashboard_app_src.is_dir():
        candidates.extend(sorted(dashboard_app_src.rglob("*.tsx")))
        candidates.extend(sorted(dashboard_app_src.rglob("*.ts")))

    return candidates


def check(repo_root: Path | None = None) -> list[tuple[Path, str, str]]:
    """Scan served frontend assets for banned external CDN origins.

    Returns a list of ``(file, line_content, matched_origin)`` tuples for every
    hit found.  An empty list means the tree is clean.

    Parameters
    ----------
    repo_root:
        Override the repo root (used in tests with a temp directory).
    """
    hits: list[tuple[Path, str, str]] = []
    for path in _scanned_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for origin in BANNED_ORIGINS:
                if origin in line:
                    hits.append((path, f"line {lineno}: {line.strip()[:200]}", origin))
    return hits


def main() -> int:
    """Entry point for the CI job.  Returns 0 on clean, 1 on violations found."""
    hits = check()
    if not hits:
        print("no-external-origins check passed: no CDN origins found in served assets")
        return 0

    print("NO-EXTERNAL-ORIGINS CHECK FAILED")
    print(
        "The following files reference external CDN origins that would cause "
        "runtime network egress — violating the air-gap posture:"
    )
    for path, location, origin in hits:
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        print(f"  [{origin}]  {rel}  {location}")

    print()
    print(
        "Fix: ensure all external URLs are replaced with bundled equivalents "
        "(npm package imports), then rebuild: cd dashboard-app && npm run build."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
