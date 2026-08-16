"""Every homelab service must record which host it runs on.

The manifest declares `host` per service — ground truth from homelab-ansible's
`inventory/host_vars/*.yml` `docker_stacks` entries. The collector read the
manifest but built its resource `data` without that field, so a service row
could never say where the service lives.

That single field is the only thing linking a service to a machine. Without it,
"what runs on media_host" and "what breaks if node_a goes down" are unanswerable,
and the knowledge graph cannot express `Service ─RUNS_ON→ Host` at all — which
is exactly the relationship class this system exists to record.

Both item shapes must carry it. A `url:null` service still runs somewhere, and
is arguably the more important case: it has no URL, so nothing else can locate it.
"""

from unittest.mock import MagicMock, patch

from infra_brain.agents.homelab_services import HomelabServicesAgent

MODULE = "infra_brain.agents.homelab_services"

_PROBEABLE = {
    "name": "grafana",
    "url": "http://203.0.113.15:3000",
    "category": "observability",
    "host": "node_a",
    "source": "iac",
}
_NO_URL = {
    "name": "litellm-db",
    "url": None,
    "category": "database",
    "host": "node_a",
    "source": "iac",
    "note": "internal container, no HTTP endpoint",
}


def _collect(entries):
    agent = HomelabServicesAgent()
    resp = MagicMock()
    resp.status_code = 200
    with (
        patch(f"{MODULE}._load_manifest", return_value=entries),
        patch(f"{MODULE}.readonly_get", return_value=resp),
    ):
        return {i["name"]: i for i in agent.collect().items}


def test_probed_service_records_its_host():
    items = _collect([_PROBEABLE])
    data = items["grafana"]["data"]
    assert data["host"] == "node_a", (
        "a probed service must record the host it runs on — without it the "
        "service cannot be linked to a machine"
    )
    assert data["status"] == "up"


def test_unprobeable_service_still_records_its_host():
    """The url:null case matters MORE — nothing else can locate these."""
    items = _collect([_NO_URL])
    data = items["litellm-db"]["data"]
    assert data["host"] == "node_a", (
        "a url:null service still runs on a machine; dropping its host makes it "
        "permanently unplaceable, since it has no URL to infer a location from"
    )
    assert data["status"] == "not_applicable"


def test_host_absent_from_manifest_is_recorded_as_none_not_omitted():
    """A genuinely host-less entry records None — an explicit unknown, not a
    missing key that a consumer would have to guess about."""
    entry = dict(_PROBEABLE)
    entry.pop("host")
    items = _collect([entry])
    data = items["grafana"]["data"]
    assert "host" in data and data["host"] is None


def test_manifest_provenance_is_carried():
    """`source` records which pass found the service (IaC vs wiki). Cheap to
    carry, and it is how a reader judges whether `host` is trustworthy."""
    assert _collect([_PROBEABLE])["grafana"]["data"]["source"] == "iac"
