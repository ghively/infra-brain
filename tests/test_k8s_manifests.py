"""k8s manifest sanity checks — no cluster required, pure YAML inspection."""

from pathlib import Path

import yaml

K8S_DIR = Path(__file__).resolve().parent.parent / "k8s"


def _container(manifest: dict, name: str) -> dict:
    containers = manifest["spec"]["template"]["spec"]["containers"]
    for c in containers:
        if c["name"] == name:
            return c
    raise AssertionError(f"no container named {name!r} in manifest")


def test_scheduler_has_liveness_and_readiness_probes():
    """#79: k8s/scheduler.yaml must have real liveness/readiness probes,
    mirroring the compose scheduler service's TRK-009 deadman healthcheck —
    a hung APScheduler thread must be detectable, same as it is today under
    docker compose."""
    manifest = yaml.safe_load((K8S_DIR / "scheduler.yaml").read_text())
    container = _container(manifest, "scheduler")
    assert "livenessProbe" in container, "scheduler container has no livenessProbe"
    assert "readinessProbe" in container, "scheduler container has no readinessProbe"
