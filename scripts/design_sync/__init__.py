from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_INDEX = REPO_ROOT / "src" / "infra_brain" / "dashboard" / "static" / "index.html"
SUPPORT_JS = REPO_ROOT / "src" / "infra_brain" / "dashboard" / "static" / "support.js"
STAGING = Path(__file__).resolve().parent / ".staging"
PROJECT_ID = "d9d71452-cbab-4e79-a168-a6bfe634da59"
PUBLISHED_DOC = "app/dashboard.dc.html"
PUBLISHED_SUPPORT = "app/support.js"
