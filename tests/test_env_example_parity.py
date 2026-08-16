"""H4 — .env.example parity test.

Asserts that every field in Settings has a corresponding entry in .env.example.
This ensures operators always have a documented example for every config option.

Fields are matched case-insensitively: Settings field ``postgres_url`` must
appear as ``POSTGRES_URL=`` (or any value) in .env.example.
"""

import re
from pathlib import Path

from infra_brain.config import Settings


def _load_env_example_keys(path: Path) -> set[str]:
    """Parse KEY=value lines from .env.example; return set of uppercased keys."""
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue
        # KEY=... or KEY= (empty value) — KEY may have inline comment after value
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped)
        if m:
            keys.add(m.group(1).upper())
    return keys


def test_all_settings_fields_in_env_example():
    """Every Settings field must have an entry in .env.example."""
    repo_root = Path(__file__).parent.parent
    env_example = repo_root / ".env.example"
    assert env_example.exists(), f".env.example not found at {env_example}"

    env_keys = _load_env_example_keys(env_example)

    # Settings fields → expected env var names (pydantic uses field name uppercased)
    settings_fields = list(Settings.model_fields.keys())
    expected_keys = {f.upper() for f in settings_fields}

    missing = expected_keys - env_keys
    assert not missing, (
        "The following Settings fields have no entry in .env.example:\n"
        + "\n".join(f"  {k}" for k in sorted(missing))
    )


def test_no_semaphore_keys_in_env_example():
    """Stale SEMAPHORE_* lines must not appear in .env.example."""
    repo_root = Path(__file__).parent.parent
    env_example = repo_root / ".env.example"
    env_keys = _load_env_example_keys(env_example)
    semaphore_keys = {k for k in env_keys if k.startswith("SEMAPHORE_")}
    assert not semaphore_keys, f"Stale SEMAPHORE_* keys found in .env.example: {semaphore_keys}"


# Keys that legitimately exist in .env.example but are NOT Settings fields:
# - AWS_* creds are read by boto3's default chain directly from the process env.
# - The docker-compose block provisions containers via ${VAR} interpolation and
#   is documented as compose-only in .env.example itself.
# - INFRA_BRAIN_MCP_ENABLE_MUTATIONS is read directly via os.getenv() in
#   mcp_server.py — same env-only class as the boto3 creds above.
#   (INFRA_BRAIN_MCP_TOKEN was removed in the MCP auth overhaul — auth is now
#   DB-backed per-key scoped keys, so it is no longer an env var at all.)
_ENV_ONLY_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
    "DATABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "N8N_BASIC_AUTH_PASSWORD",
    "BWS_ACCESS_TOKEN",
    "INFRA_BRAIN_MCP_ENABLE_MUTATIONS",
    # HOST-side bind-mount sources consumed by docker/docker-compose.deploy.yml,
    # never by Settings. The app only ever sees the CONTAINER-side path
    # (ANSIBLE_INVENTORY_PATH, which IS a Settings field). Added with TRK-328,
    # where these two mounts living only in the never-CI-loaded homelab overlay
    # let one manual `docker compose up -d` blind the linux collector for 8 days.
    "ANSIBLE_INVENTORY_HOST_PATH",
    "ANSIBLE_DEPLOY_KEY_HOST_PATH",
}


def test_all_env_example_keys_are_settings_fields():
    """F-036 reverse direction: every .env.example key must be a real Settings
    field (or an explicitly allowlisted compose/boto3 key). Catches vars that
    operators can set but that ``extra="ignore"`` silently drops."""
    repo_root = Path(__file__).parent.parent
    env_keys = _load_env_example_keys(repo_root / ".env.example")
    settings_keys = {f.upper() for f in Settings.model_fields.keys()}
    orphans = env_keys - settings_keys - _ENV_ONLY_KEYS
    assert not orphans, (
        ".env.example declares vars that Settings silently ignores "
        "(extra='ignore'):\n" + "\n".join(f"  {k}" for k in sorted(orphans))
    )
