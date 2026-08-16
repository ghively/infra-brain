"""Seeds Postgres from infra-ops knowledge/ on first run."""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# The corporate Brain DB lives in the corporate zone; the infra-ops ledger no longer
# carries a zone level in its path, so every seeded instinct is corporate by
# construction. Matches ZONE_CORPORATE / the Instinct.zone default in
# db/models/core.py and DEFAULT_ZONE in the sibling instinct scripts.
DEFAULT_ZONE = "corpor"


def _infra_ops_root() -> Path | None:
    """Read INFRA_OPS_ROOT from the environment; there is no meaningful default location
    for a sibling checkout of infra-ops, and a hardcoded one silently no-ops every
    .exists() check below. Unset or missing means the knowledge-derived seeding is
    skipped — loudly, via a WARNING, never silently."""
    raw = os.getenv("INFRA_OPS_ROOT")
    if not raw:
        log.warning(
            "INFRA_OPS_ROOT is not set — skipping scan_point and instinct seeding "
            "from the infra-ops knowledge/ tree"
        )
        return None
    root = Path(raw)
    if not root.exists():
        log.warning(
            "INFRA_OPS_ROOT=%s does not exist — skipping scan_point and instinct "
            "seeding from the infra-ops knowledge/ tree",
            root,
        )
        return None
    return root


def parse_discovery_coverage(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("scan_points", [])


def parse_instincts_dir(instincts_root: str) -> list[dict]:
    """Parse the flat ``<domain>/*.yml`` instinct ledger. The zone level was removed
    from infra-ops, so every entry is attributed to DEFAULT_ZONE."""
    results = []
    root = Path(instincts_root)
    for domain_dir in sorted(root.iterdir()):
        if not domain_dir.is_dir():
            continue
        domain = domain_dir.name
        for yml_file in sorted(domain_dir.glob("*.yml")):
            with open(yml_file) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            results.append(
                {
                    "zone": DEFAULT_ZONE,
                    "domain": domain,
                    "pattern": data.get("claim") or data.get("pattern", ""),
                    "confidence": float(data.get("confidence", 0.7)),
                    "promoted_by": data.get("promoted_by", "seed"),
                    "citation": data.get("citation"),
                }
            )
    return results


def seed(session) -> None:
    from infra_brain.db.models import ScanPoint, Instinct

    root = _infra_ops_root()
    knowledge_dir = root / "knowledge" if root else None
    discovery_coverage = knowledge_dir / "discovery-coverage.yml" if knowledge_dir else None
    instincts_dir = knowledge_dir / "instincts" if knowledge_dir else None

    # Seed scan_points
    existing_scans = {sp.domain + sp.endpoint for sp in session.query(ScanPoint).all()}
    if discovery_coverage and discovery_coverage.exists():
        skipped = 0
        for sp_data in parse_discovery_coverage(str(discovery_coverage)):
            # infra-ops's discovery-coverage.yml has schema-drifted away from the
            # ScanPoint model: entries now carry id/category/inspects/method, not
            # domain/endpoint/schedule. The two also mean different things — this
            # registry records what discovery is AUTHORISED to inspect, while
            # ScanPoint records collection scheduling. Skip loudly rather than
            # invent a mapping that conflates them; reconciling the two is its own
            # piece of work (CONVERGENCE-PLAN P3.2).
            if "domain" not in sp_data:
                skipped += 1
                continue
            key = sp_data["domain"] + sp_data.get("endpoint", "")
            if key not in existing_scans:
                session.add(
                    ScanPoint(
                        id=uuid.uuid4(),
                        domain=sp_data["domain"],
                        method=sp_data.get("method", ""),
                        endpoint=sp_data.get("endpoint", ""),
                        schedule=sp_data.get("schedule", "0 */1 * * *"),
                        status=sp_data.get("status", "active"),
                    )
                )
        if skipped:
            print(
                f"WARNING {skipped} scan_point entr{'y' if skipped == 1 else 'ies'} in "
                f"{discovery_coverage} lack a 'domain' field and were skipped — the file's "
                "schema no longer matches the ScanPoint model."
            )
        print(f"Seeded scan_points from {discovery_coverage}")
    elif discovery_coverage:
        log.warning("%s does not exist — skipping scan_point seeding", discovery_coverage)

    # Seed instincts
    if instincts_dir and instincts_dir.exists():
        existing_instincts = {
            i.zone + i.domain + i.pattern[:32] for i in session.query(Instinct).all()
        }
        for inst_data in parse_instincts_dir(str(instincts_dir)):
            key = inst_data["zone"] + inst_data["domain"] + inst_data["pattern"][:32]
            if key not in existing_instincts:
                session.add(
                    Instinct(
                        id=uuid.uuid4(),
                        zone=inst_data["zone"],
                        domain=inst_data["domain"],
                        pattern=inst_data["pattern"],
                        confidence=inst_data["confidence"],
                        promoted_by=inst_data["promoted_by"],
                        promoted_at=datetime.now(timezone.utc),
                        citation=inst_data.get("citation"),
                    )
                )
        print(f"Seeded instincts from {instincts_dir}")
    elif instincts_dir:
        log.warning("%s does not exist — skipping instinct seeding", instincts_dir)

    # Seed IntegrationAgent daily sweep if not present
    existing_integration = session.query(ScanPoint).filter_by(domain="integration").first()
    if not existing_integration:
        session.add(
            ScanPoint(
                id=uuid.uuid4(),
                domain="integration",
                method="agent",
                endpoint="all",
                schedule="0 2 * * *",  # 2 AM daily
                status="active",
            )
        )

    # Create admin UIUser from settings if not already present
    from infra_brain.config import get_settings

    _s = get_settings()
    admin_username = _s.admin_username
    admin_password = _s.admin_password
    if admin_password:
        import bcrypt
        from infra_brain.db.models import UIUser

        existing = session.query(UIUser).filter_by(username=admin_username).first()
        if not existing:
            pw_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
            session.add(
                UIUser(
                    id=uuid.uuid4(),
                    username=admin_username,
                    name=admin_username,
                    password_hash=pw_hash,
                    role="admin",
                    active=True,
                )
            )
            print(f"Created admin user: {admin_username}")

    session.commit()
    print("Seed complete.")


if __name__ == "__main__":
    # NOTE: do NOT call create_tables()/Base.metadata.create_all() here. In prod,
    # alembic is the SOLE schema authority — `alembic upgrade head` runs in the
    # migrate service before seed. create_all() only issues CREATE TABLE for
    # missing tables and NEVER issues ALTER on an existing one, so it silently
    # masks column drift (this is exactly how the r7_vulnerabilities.resource_id
    # 500 shipped green). The schema must already be fully migrated when seed runs.
    # create_all() is retained ONLY in test fixtures (tests/conftest.py, SQLite).
    from infra_brain.db.session import get_session

    with get_session() as session:
        seed(session)
