import logging
from datetime import timedelta

from infra_brain.db.models import CloudResource, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectOutcome, CollectorSkipped, ETLConnector
from infra_brain.tools.aws import (
    aws_ec2_instances_tool,
    aws_ec2_regions_tool,
    aws_security_groups_tool,
    aws_vpcs_tool,
)
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# The provider id field carried in ``data`` per emitted type. The relational
# natural key is (provider, cloud_id), so each type contributes its stable AWS id.
_CLOUD_ID_FIELD = {
    "ec2_instance": "instance_id",
    "vpc": "vpc_id",
    "security_group": "group_id",
}
# data fields already mapped to a typed column → excluded from the details overflow.
_MAPPED_FIELDS = {"region", "state"}


class CloudAgent(ETLConnector):
    spec = AgentSpec(
        domain="cloud",
        tier=Tier.COLLECTOR,
        schedule="0 2 * * *",
        max_staleness=timedelta(hours=26),
        # 2026-08-12: retired (was POC-deferred 2026-07-15 via dispatchable=False
        # plus a commented-out supervisor._AGENT_SPECS entry). There is no AWS
        # account behind this home lab — "deferred until post-POC" has become a
        # standing decision, so it is now declared as one. Migrated onto the
        # first-class switch so the domain stays VISIBLE in the roster as
        # deliberately off rather than vanishing from AGENT_REGISTRY entirely,
        # and so re-enabling is one setting rather than two source edits:
        # COLLECTION_REVIVED_DOMAINS=cloud (plus aws_enabled=True, which
        # collect() still enforces on its own).
        #
        # P5 REVIVAL OBLIGATION: cloud is a HOST-BEARING leg
        # (host_reconcile._SOURCE_KEYS "cloud"). Reviving it WITHOUT adding a
        # NodeSpec(..., is_host_identity=True) here AND the matching entry in
        # graph_phase3.HOST_NODE_TYPES leaves every cloud↔anything identity
        # pair invisible to the resolver — not queued for a human, silently
        # absent. No speculative NodeSpec is declared now (zero rows, ever).
        retired=True,
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        if not getattr(self.settings, "aws_enabled", False):
            raise CollectorSkipped("aws_enabled=False")

        _cb = {"callbacks": self.callbacks}
        items = []
        errors: list[str] = []

        try:
            regions = aws_ec2_regions_tool.invoke({}, config=_cb)
        except Exception as exc:
            msg = f"failed to list EC2 regions: {exc}"
            logger.warning("CloudAgent: %s", msg)
            errors.append(msg)
            regions = []

        for region in regions:
            try:
                items.extend(aws_ec2_instances_tool.invoke({"region": region}, config=_cb))
                items.extend(aws_vpcs_tool.invoke({"region": region}, config=_cb))
                items.extend(aws_security_groups_tool.invoke({"region": region}, config=_cb))
            except Exception as exc:
                msg = f"error collecting EC2 resources in {region}: {exc}"
                logger.warning("CloudAgent: %s", msg)
                errors.append(msg)

        # Cache for the relational detail-write phase so we don't re-query AWS.
        self._last_items = items
        return CollectOutcome(items=items, errors=errors)

    # ------------------------------------------------------------------
    # Relational detail-write phase (MR5)
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        # Surface a detail-write failure on the CollectionRun (no silent data
        # loss) via ETLConnector.run()'s _write_details.
        return [self._write_cloud_details]

    def _write_cloud_details(self) -> int:
        """Populate cloud_resources from the items collect() emitted.

        Idle-safety FIRST: when AWS is disabled the collector never ran — there
        is nothing to write and we must not touch the DB.

        Returns the number of detail rows written so ``_write_details`` can
        persist it to ``CollectionRun.detail_rows_written`` (GitLab #148: a
        detail writer that returns None makes the counter stay 0 forever even
        though the relational table was populated — indistinguishable from the
        write path being dead; see vsphere.py's ``_write_vsphere_details`` for
        the same fix applied there).
        """
        if not getattr(self.settings, "aws_enabled", False):
            logger.info("CloudAgent: aws_enabled=False — relational write skipped (idle)")
            return 0

        items = getattr(self, "_last_items", None) or []
        if not items:
            return 0

        with get_session() as session:
            written, skipped = self._write_each(
                session,
                items,
                lambda item: self._upsert_cloud_item(session, item),
                label_fn=lambda item, exc: (
                    f"CloudAgent: skipping bad {item.get('type')} item {item.get('name')!r}: {exc}"
                ),
            )
            self._cloud_details_written = written
            self._cloud_details_skipped = skipped
            session.commit()

        return written

    def _upsert_cloud_item(self, session, item: dict) -> None:
        cloud_type = item.get("type", "")
        data = item.get("data", {}) or {}
        name = item.get("name", "")

        id_field = _CLOUD_ID_FIELD.get(cloud_type)
        if id_field is None:
            raise ValueError(f"no cloud_id mapping for type {cloud_type!r}")
        cloud_id = data.get(id_field)
        if not cloud_id:
            raise ValueError(f"no {id_field} on {cloud_type} {name!r} — cannot key relational row")

        res = session.query(Resource).filter_by(domain="cloud", type=cloud_type, name=name).first()
        details = {k: v for k, v in data.items() if k not in (_MAPPED_FIELDS | {id_field})}
        row = {
            "resource_id": res.id if res else None,
            "provider": "aws",
            "cloud_type": cloud_type,
            "cloud_id": cloud_id,
            "name": name,
            "region": data.get("region"),
            "state": data.get("state"),
            "details": details or None,
        }
        self._upsert_detail(session, CloudResource, row, ["provider", "cloud_id"])
