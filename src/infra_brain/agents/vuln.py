"""VulnAgent — Rapid7 InsightVM collector + vuln_queue feeder.

``collect()`` upserts the generic ``r7_asset`` ``Resource`` rows (inventory +
EOL OS-string source + the feeder asset list). It deliberately no longer pulls
the unbounded global Rapid7 vuln catalog (``rapid7_vulns_all_tool``, ~100k+
defs) — that exceeded the 600s ``collect_timeout`` so ``run()`` never reached
the detail-write phase and ``vuln_queue``/``eol_registry`` stayed empty. The
``run()`` override then runs a detail-write phase that
FEEDS the ``vuln_queue`` table from the asset↔CVE linkage Rapid7 exposes at
``/api/3/assets/{id}/vulnerabilities`` (the previously-unused
``rapid7_asset_vulnerabilities_tool``).

Bounding (critical): the fleet has ~2586 assets and the collect() wall-clock is
300-600s, so we never fetch per-asset vulns for every asset. We consider only
assets with ``vulnerabilities_count > 0`` and cap to the top-N by ``risk_score``
(``RAPID7_VULN_ASSET_CAP``, default 750). How many were scanned vs skipped is
LOGGED — no silent truncation. Per-asset fetches run through a small bounded
thread pool.

Coverage rotation (GitLab #173 / #188 Bug 2 — the cap's self-heal gap): a pure
top-N-by-risk cap means an asset that never re-enters the top-750 NEVER gets a
``vuln_queue`` refresh, so its severity/CVE rows go stale indefinitely (the
manual ``scripts/backfill_vuln_queue_severity.py`` was the only escape hatch).
When the cap is binding, ``_bounded_assets`` now reserves
``RAPID7_VULN_ROTATION_SLOTS`` (default 150) of the cap's budget for a
round-robin slice over the assets ranked BELOW the guaranteed top
``cap - slots``: the slice's starting offset advances by ``slots`` positions
per run (run ordinal derived from this domain's ``collection_runs`` count —
the same schema-free mechanism as NetDiscoveryAgent's #178 tail-starvation
fix), so every below-the-line asset is re-selected at least once every
``ceil(R / slots)`` runs (R = assets outside the guaranteed slice; with the
fleet above, roughly weekly on the daily schedule) instead of never. When the
asset list fits under the cap the selection is byte-identical to the old
behavior — rotation only redistributes scarcity, it never creates it.

Upsert key is (resource_id, cve_id). ``status`` is set to "open" only for NEW
rows — an existing row's triage/remediated/accepted status is preserved; only
severity / last_updated are refreshed on re-run.

SLA anchoring (GitLab #136): ``sla_due`` is derived from the finding's
FIRST-SEEN date (Rapid7's per-finding ``since`` timestamp, falling back to the
insert time) and is IMMUTABLE across rescans — a rescan re-confirming an
existing finding refreshes ``last_updated`` only, never the deadline. The old
behavior (``sla_due = now + window`` recomputed on every upsert) reset the SLA
clock on each daily rescan, so no CVE on a rescanned asset could ever breach
its SLA. The only permitted change to an existing ``sla_due`` is a TIGHTENING
when enrichment upgrades the severity band (e.g. the low placeholder →
critical); it can never be extended.

CVE id (critical): Rapid7's asset-vuln ``id`` is a long slug (e.g.
``microsoft-dot_net_framework-cve-2026-23666``) that overflows ``cve_id``'s
``String(32)`` → ``StringDataRightTruncation`` → the whole detail-write batch
rolled back (vuln_queue=0, eol=0). We extract the canonical CVE id instead
(``cves`` array if present, else a ``CVE-\\d{4}-\\d{4,}`` regex over the slug),
fan a multi-CVE finding into one row per (resource_id, cve), skip+count non-CVE
checks (this is a CVE queue), and guard each row in its own SAVEPOINT so one bad
record can't abort the batch.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial

from infra_brain.agents._cve import extract_cves
from infra_brain.agents.vuln_cve import VulnCveBridge
from infra_brain.db import severity as sev
from infra_brain.db.models import (
    AgentActionLog,
    CollectionRun,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetSite,
    R7AssetUser,
    R7Site,
    R7Software,
    R7Tag,
    R7Vulnerability,
    Resource,
    VulnQueueItem,
)
from infra_brain.db.session import get_session
from infra_brain.db.vuln_status import CLOSED_AUTO, OPEN_VULN_STATUSES
from infra_brain.etl.base import (
    CollectionResult,
    CollectorSkipped,
    ETLConnector,
    ReconcileScope,
)
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.rapid7 import (
    _unconfigured,
    rapid7_asset_vulnerabilities_tool,
    rapid7_assets_all_tool,
    rapid7_sites_tool,
    rapid7_tags_tool,
)

logger = logging.getLogger(__name__)

# Severity band values, SLA windows, and ranking all come from the canonical
# db/severity.py module (imported as ``sev``). Do NOT reintroduce local severity
# dicts here — readers elsewhere silently zeroed out when the casing drifted
# (the June-2026 bug).

# Bounded fan-out for the per-asset vuln fetches and the vuln-detail enrichment.
_FETCH_WORKERS = 8

# CVE-id extraction lives in agents/_cve.py (shared with VulnCveBridge).


class VulnAgent(ETLConnector):
    spec = AgentSpec(
        domain="vuln",
        tier=Tier.COLLECTOR,
        schedule="15 2 * * *",
        max_staleness=timedelta(hours=26),
        # 2026-08-12: retired. Rapid7 InsightVM is an enterprise remnant with no
        # home-lab counterpart. docs/TRACKER.md TRK-316 records a live forensic
        # pass: every Rapid7-backed table is EMPTY (not "0 closed of many" —
        # empty), and all 8 `vuln` runs in DB history are status='skipped' with
        # "rapid7_url or rapid7_api_key not configured". Re-enable with
        # COLLECTION_REVIVED_DOMAINS=vuln.
        #
        # This also retires VulnCveBridge (agents/vuln_cve.py) by construction:
        # it is not a registered domain, it is a detail-writer this agent calls,
        # so it stops when this stops. VulnTriageAgent is deliberately NOT
        # retired — see its own spec comment.
        retired=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        self._last_assets = []
        if _unconfigured():
            raise CollectorSkipped("rapid7_url or rapid7_api_key not configured")

        _cb = {"callbacks": self.callbacks}
        items = []
        assets_raw: list[dict] = []

        # F-007: a total asset-fetch failure must raise -> status="failed";
        # the old swallow masked a dead Rapid7 as an empty-but-successful run.
        assets_raw = rapid7_assets_all_tool.invoke({}, config=_cb)
        for asset in assets_raw:
            fp = self._fingerprint_fields(asset)
            items.append(
                {
                    "name": asset.get("hostName") or asset.get("ip", str(asset.get("id", ""))),
                    "type": "r7_asset",
                    "data": {
                        "asset_id": asset.get("id"),
                        "ip": asset.get("ip", ""),
                        "hostname": asset.get("hostName", ""),
                        # FIX (MR6): the Rapid7 list-item ``os`` is a STRING,
                        # not a dict. The old ``asset["os"]["name"]`` path
                        # always yielded "" and starved the EOL deriver.
                        "os": asset.get("os") or "",
                        "os_family": fp["os_family"],
                        "os_product": fp["os_product"],
                        "os_version": fp["os_version"],
                        "os_vendor": fp["os_vendor"],
                        "os_architecture": fp["os_architecture"],
                        "risk_score": asset.get("riskScore", 0),
                        "vulnerabilities": self._vuln_count(asset),
                    },
                }
            )

        # NOTE: the global Rapid7 vuln catalog (rapid7_vulns_all_tool, ~100k+
        # unbounded definitions) is deliberately NOT pulled here. It blew past
        # the collect_timeout (600s), so run() never reached the _write_vuln_queue
        # detail-write phase and vuln_queue/eol_registry stayed empty. The
        # per-host CVE linkage feeds vuln_queue in the detail-write phase below,
        # so the global r7_vulnerability rows are redundant. Keep only the
        # bounded asset pull (inventory + eol OS-string source + feeder list).

        # Stash the raw asset list so the detail-write phase can bound + fetch
        # per-asset vulns without re-paginating the whole asset catalog.
        self._last_assets = assets_raw
        return items

    # --- vuln_queue feeder -------------------------------------------------

    def _detail_writers(self, scope, result):
        # Ordered detail-write phases, each surfaced on the CollectionRun by
        # ETLConnector.run() via _write_details (no silent data loss): (1) rich
        # relational r7_assets + child tables (software/config/users/addresses) +
        # sites/tags, (2) the MR3 vuln_queue feeder (harvests the distinct vuln
        # slugs), (3) the MR7 vuln-detail/solutions enrichment, (4) GitLab #173 /
        # #188 Bug 3 same-run severity reconciliation (see
        # _reconcile_unenriched_severity). A fifth phase, ``_write_graph_edges``,
        # stood here until P5 deleted it with the ``resource_relationships``
        # store it was the only writer into — see its epitaph at the foot of
        # this file. Order MATTERS: phase
        # 3 consumes the slugs phase 2 collects on ``self._distinct_vuln_slugs``,
        # and phase 4 consumes the (resource_id, cve_id, slug) rows phase 2
        # wrote with the "not yet enriched" placeholder, re-resolving them
        # against whatever phase 3 just enriched.
        #
        # C-1: phase 2 is bound to ``result`` so its swallowed per-asset fetch
        # failures can be reported onto the run (status "partial" + error_message)
        # instead of vanishing. ``result`` is optional on the method, so direct
        # unit-test calls of ``_write_vuln_queue()`` still work — the destructive
        # -pass narrowing (the safety-critical half) does not depend on it.
        return [
            self._write_r7_relational,
            partial(self._write_vuln_queue, result),
            self._write_vuln_details,
            self._reconcile_unenriched_severity,
        ]

    # --- osFingerprint extraction -----------------------------------------

    @staticmethod
    def _fingerprint_fields(asset: dict) -> dict:
        """Break out the Rapid7 ``osFingerprint`` dict into typed os_* fields.

        ``osFingerprint`` is the structured dict
        ({family, product, version, vendor, architecture, type, description,
        systemName}); it is ``None`` for external/internet noise IPs. Returns a
        dict of the five fields we keep as typed columns (each ``None`` if the
        fingerprint is absent).
        """
        fp = asset.get("osFingerprint")
        if not isinstance(fp, dict):
            fp = {}
        return {
            "os_family": fp.get("family"),
            "os_product": fp.get("product"),
            "os_version": fp.get("version"),
            "os_vendor": fp.get("vendor"),
            "os_architecture": fp.get("architecture"),
        }

    @staticmethod
    def _is_real_asset(asset: dict) -> bool:
        """Noise filter: a real fleet asset has a fingerprint, risk, or a type.

        External/internet IPs come back with ``osFingerprint=None``,
        ``riskScore=0``, and ``type=None`` — not fleet assets. Keep an asset when
        ANY of: osFingerprint is not null, riskScore > 0, or type is not null.
        """
        if isinstance(asset.get("osFingerprint"), dict):
            return True
        try:
            if float(asset.get("riskScore") or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        return asset.get("type") is not None

    @staticmethod
    def _vuln_count(asset: dict) -> int:
        v = asset.get("vulnerabilities")
        if isinstance(v, dict):
            return v.get("total", 0) or 0
        if isinstance(v, (int, float)):
            return int(v)
        return 0

    # --- r7_assets / r7_sites / r7_tags relational writer (MR6) ------------

    @staticmethod
    def _to_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _vuln_bands(asset: dict) -> dict:
        """The six Rapid7 vuln-band counts (critical/severe/moderate — NO high)."""
        v = asset.get("vulnerabilities")
        if not isinstance(v, dict):
            v = {}

        def _n(key) -> int:
            try:
                return int(v.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "vuln_critical": _n("critical"),
            "vuln_severe": _n("severe"),
            "vuln_moderate": _n("moderate"),
            "vuln_total": _n("total"),
            "vuln_exploits": _n("exploits"),
            "vuln_malware_kits": _n("malwareKits"),
        }

    def _asset_row(self, asset: dict, resource_id) -> dict:
        """Map a Rapid7 list-item asset → an ``r7_assets`` upsert row dict.

        ``os`` is the STRING list-item field (the bug-fixed capture); the os_*
        columns come from ``osFingerprint``; the six vuln bands from the
        ``vulnerabilities`` dict; the large nested lists go into ``details``.
        """
        fp = self._fingerprint_fields(asset)
        row = {
            "resource_id": resource_id,
            "r7_asset_id": int(asset["id"]),
            "ip": asset.get("ip"),
            "hostname": asset.get("hostName"),
            "mac": asset.get("mac"),
            "os": asset.get("os"),
            "asset_type": asset.get("type"),
            "risk_score": self._to_float(asset.get("riskScore")),
            "raw_risk_score": self._to_float(asset.get("rawRiskScore")),
            "assessed_for_vulnerabilities": bool(asset.get("assessedForVulnerabilities", False)),
            "details": {
                k: asset.get(k)
                for k in ("software", "users", "configurations", "addresses", "ids", "hostNames")
                if asset.get(k) is not None
            }
            or None,
        }
        row.update(fp)
        row.update(self._vuln_bands(asset))
        return row

    def _write_r7_relational(self) -> int:
        """Populate r7_assets (noise-filtered) + r7_sites + r7_tags from the
        already-pulled asset list plus the cheap global sites/tags endpoints.

        No per-asset detail calls — everything for r7_assets comes from the list
        item that ``collect()`` already stashed on ``self._last_assets``.

        DL-C-5/DL-C-6: each asset (upsert + child-table rewrite) is guarded by its
        own SAVEPOINT so one bad row (e.g. a bad child-table value) rolls back only
        that asset, never the whole batch — the same class of all-or-nothing
        rollback that zeroed ``vuln_queue`` before the MR3 SAVEPOINT fix there.
        Returns the count of assets actually written so ``_write_details``
        accumulates it into ``CollectionResult.detail_rows_written`` — a partial
        write (some assets guarded out) is then visible on the run's stats instead
        of silently reading as a full success.
        """
        assets = getattr(self, "_last_assets", None) or []
        _cb = {"callbacks": self.callbacks}

        # --- r7_assets (noise-filtered) ---
        real = [a for a in assets if a.get("id") is not None and self._is_real_asset(a)]
        noise = len(assets) - len(real)
        logger.info(
            "VulnAgent: r7_assets — %d assets total, %d real, %d filtered as network-noise",
            len(assets),
            len(real),
            noise,
        )
        written = 0
        failed = 0
        with get_session() as session:
            # Map the canonical r7_asset Resource id (collect() upserted it keyed
            # by hostName/ip — the same name we rebuild here) so r7_assets links
            # back to resources.
            for asset in real:
                name = asset.get("hostName") or asset.get("ip") or str(asset.get("id", ""))
                try:
                    with session.begin_nested():
                        res = (
                            session.query(Resource)
                            .filter_by(domain="vuln", type="r7_asset", name=name)
                            .first()
                        )
                        resource_id = res.id if res is not None else None
                        self._upsert_detail(
                            session, R7Asset, self._asset_row(asset, resource_id), ["r7_asset_id"]
                        )
                        # The upsert added/updated the R7Asset; flush so a freshly-added
                        # row gets its PK, then look it up to link the child tables.
                        session.flush()
                        r7_asset = (
                            session.query(R7Asset).filter_by(r7_asset_id=int(asset["id"])).first()
                        )
                        if r7_asset is not None:
                            self._write_asset_children(session, r7_asset, asset)
                    written += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "VulnAgent: r7_assets write failed for asset id=%s, skipped: %s",
                        asset.get("id"),
                        exc,
                    )
            session.commit()
        logger.info(
            "VulnAgent: r7_assets — %d written, %d failed and guarded out (of %d real assets)",
            written,
            failed,
            len(real),
        )

        # --- r7_sites (global, cheap) ---
        try:
            sites = rapid7_sites_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("VulnAgent: failed to fetch r7 sites: %s", exc)
            sites = []
        with get_session() as session:
            for site in sites:
                if site.get("id") is None:
                    continue
                self._upsert_detail(session, R7Site, self._site_row(site), ["r7_site_id"])
            session.commit()
        logger.info("VulnAgent: r7_sites — upserted %d sites", len(sites))

        # --- r7_tags (global, cheap) ---
        try:
            tags = rapid7_tags_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("VulnAgent: failed to fetch r7 tags: %s", exc)
            tags = []
        with get_session() as session:
            for tag in tags:
                if tag.get("id") is None:
                    continue
                self._upsert_detail(session, R7Tag, self._tag_row(tag), ["r7_tag_id"])
            session.commit()
        logger.info("VulnAgent: r7_tags — upserted %d tags", len(tags))

        return written

    # --- r7_assets child tables (MR7, from the asset LIST data, no API) ----

    @staticmethod
    def _trunc(value, length: int):
        """Best-effort coerce-to-str + truncate so a long value can't overflow a
        typed column (the same class of bug as the vuln-slug truncation)."""
        if value is None:
            return None
        s = str(value)
        return s[:length] if len(s) > length else s

    def _write_asset_children(self, session, r7_asset, asset: dict) -> None:
        """Populate the four r7_assets child tables from the asset list-item.

        Pure DB writes — every field comes from the ``software`` /
        ``configurations`` / ``users`` / ``addresses`` lists Rapid7 already
        returned in the asset list (no per-asset API calls). Delete-then-reinsert
        per asset (the LinuxHost child pattern) keeps each child set idempotent and
        in sync with the latest scan. Rarely-queried extras (software type/desc,
        user ids, ...) overflow into the per-row ``details`` JSONB where present.
        Fleet-wide these are ~190k software + ~100k config rows — expected; this is
        pure DB write, no API cost.
        """
        asset_id = r7_asset.id

        # --- r7_software ---
        session.query(R7Software).filter_by(asset_id=asset_id).delete()
        seen_sw: set[tuple] = set()
        for sw in asset.get("software") or []:
            if not isinstance(sw, dict):
                continue
            product = self._trunc(sw.get("product"), 256)
            if not product:
                continue
            version = self._trunc(sw.get("version"), 128)
            natkey = (product, version)
            if natkey in seen_sw:  # the unique key is (asset_id, product, version)
                continue
            seen_sw.add(natkey)
            extras = {
                k: sw.get(k)
                for k in ("id", "type", "description", "family", "configurations")
                if sw.get(k) is not None
            }
            session.add(
                R7Software(
                    asset_id=asset_id,
                    r7_asset_id=int(asset["id"]),
                    product=product,
                    vendor=self._trunc(sw.get("vendor"), 128),
                    version=version,
                    software_type=self._trunc(sw.get("type") or sw.get("description"), 64),
                    details=extras or None,
                )
            )

        # --- r7_asset_config (system/hardware specs) ---
        session.query(R7AssetConfig).filter_by(asset_id=asset_id).delete()
        seen_cfg: set[str] = set()
        for cfg in asset.get("configurations") or []:
            if not isinstance(cfg, dict):
                continue
            name = self._trunc(cfg.get("name"), 256)
            if not name or name in seen_cfg:  # unique key is (asset_id, name)
                continue
            seen_cfg.add(name)
            value = cfg.get("value")
            session.add(
                R7AssetConfig(
                    asset_id=asset_id,
                    name=name,
                    value=str(value) if value is not None else None,
                )
            )

        # --- r7_asset_users ---
        session.query(R7AssetUser).filter_by(asset_id=asset_id).delete()
        seen_user: set[str] = set()
        for usr in asset.get("users") or []:
            if not isinstance(usr, dict):
                continue
            # The list-item ``name`` is the username; ``fullName`` if present.
            username = self._trunc(usr.get("name") or usr.get("id"), 256)
            if not username or username in seen_user:  # unique key is (asset_id, username)
                continue
            seen_user.add(username)
            session.add(
                R7AssetUser(
                    asset_id=asset_id,
                    username=username,
                    full_name=self._trunc(usr.get("fullName") or usr.get("full_name"), 256),
                )
            )

        # --- r7_asset_addresses ---
        session.query(R7AssetAddress).filter_by(asset_id=asset_id).delete()
        seen_addr: set[str] = set()
        for addr in asset.get("addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip = self._trunc(addr.get("ip"), 64)
            if not ip or ip in seen_addr:  # unique key is (asset_id, ip)
                continue
            seen_addr.add(ip)
            session.add(
                R7AssetAddress(
                    asset_id=asset_id,
                    ip=ip,
                    mac=self._trunc(addr.get("mac"), 64),
                )
            )

        # --- r7_asset_sites (TRK-104: many-to-many asset↔site membership) ---
        # The asset list-item carries a ``sites`` array of upstream integer
        # site ids. Delete-then-reinsert per asset keeps the bridge idempotent
        # and in sync with the latest scan, matching the other child tables.
        r7_asset_id = int(asset["id"])
        session.query(R7AssetSite).filter_by(r7_asset_id=r7_asset_id).delete()
        seen_site: set[int] = set()
        for site_id in asset.get("sites") or []:
            try:
                sid = int(site_id)
            except (TypeError, ValueError):
                continue
            if sid in seen_site:  # unique key is (r7_asset_id, r7_site_id)
                continue
            seen_site.add(sid)
            session.add(R7AssetSite(r7_asset_id=r7_asset_id, r7_site_id=sid))

    @staticmethod
    def _parse_dt(value):
        if not value:
            return None
        from datetime import datetime as _dt

        try:
            return _dt.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _site_row(self, site: dict) -> dict:
        return {
            "r7_site_id": int(site["id"]),
            "name": site.get("name") or "",
            "asset_count": site.get("assets"),
            "risk_score": self._to_float(site.get("riskScore")),
            "importance": site.get("importance"),
            "site_type": site.get("type"),
            "last_scan_time": self._parse_dt(site.get("lastScanTime")),
            "details": {"vulnerabilities": site["vulnerabilities"]}
            if isinstance(site.get("vulnerabilities"), dict)
            else None,
        }

    @staticmethod
    def _tag_row(tag: dict) -> dict:
        return {
            "r7_tag_id": int(tag["id"]),
            "name": tag.get("name") or "",
            "tag_type": tag.get("type"),
            "color": tag.get("color"),
            "source": tag.get("source"),
        }

    @staticmethod
    def _apply_cap_with_rotation(
        ranked: list[dict], cap: int, slots: int, run_ordinal: int
    ) -> list[dict]:
        """Cap ``ranked`` (risk-desc) to ``cap``, reserving ``slots`` of the
        budget for round-robin coverage of the below-the-line assets.

        GitLab #173 / #188 Bug 2: a pure ``ranked[:cap]`` means whichever
        assets sort below the cap are the SAME assets every run (risk scores
        are sticky), so their ``vuln_queue`` rows are never refreshed — the
        structural self-heal gap both issues left open. Instead:

        - the top ``cap - slots`` by risk are ALWAYS selected (the guaranteed
          slice — highest-risk assets never lose freshness to fairness);
        - the remaining ``slots`` are filled from the rest of the ranked list,
          starting at an offset that advances by ``slots`` positions per run
          (``run_ordinal * slots % remainder``), so consecutive runs walk
          disjoint windows and every below-the-line asset is selected at least
          once every ``ceil(R / slots)`` runs for a stable remainder of size R.

        ``run_ordinal=0`` (unit tests / ordinal-read failure) starts the
        window at the highest-risk remainder — i.e. exactly the old
        ``ranked[:cap]`` behavior. ``slots<=0`` disables rotation entirely.
        When ``len(ranked) <= cap`` nothing is dropped and the list is
        returned unchanged — rotation only redistributes scarcity.
        """
        if len(ranked) <= cap:
            return list(ranked)
        slots = max(0, min(slots, cap))
        if slots == 0:
            return list(ranked[:cap])
        guaranteed = list(ranked[: cap - slots])
        remainder = list(ranked[cap - slots :])
        offset = (run_ordinal * slots) % len(remainder)
        rotated = remainder[offset:] + remainder[:offset]
        return guaranteed + rotated[:slots]

    def _coverage_rotation_ordinal(self) -> int:
        """Monotonic run counter for the rotation offset (schema-free).

        Counts this domain's non-skipped ``collection_runs`` rows (the current
        in-progress run included — ``ETLConnector.run()`` creates its row
        before the detail-write phase reaches here), mirroring
        NetDiscoveryAgent's #178 ``run_ordinal`` mechanism. ``skipped`` runs
        (Rapid7 unconfigured / collection disabled) are excluded so an
        alternating skip/run pattern can't park the rotation window on the
        same offset. Best-effort: any read failure logs and returns 0, which
        degrades to the old top-cap selection for this run only.
        """
        try:
            with get_session() as session:
                return (
                    session.query(CollectionRun)
                    .filter(
                        CollectionRun.domain == self.domain,
                        CollectionRun.status != "skipped",
                    )
                    .count()
                )
        except Exception:
            logger.exception(
                "VulnAgent: rotation-ordinal read failed — coverage rotation "
                "degrades to top-cap selection for this run"
            )
            return 0

    def _bounded_assets(self, assets: list[dict]) -> list[dict]:
        """Only assets with vulns, capped to top-N by risk_score with a
        reserved coverage-rotation slice (see ``_apply_cap_with_rotation``).
        Logs the bound."""
        cap = self.settings.rapid7_vuln_asset_cap or 750
        try:
            slots = int(self.settings.rapid7_vuln_rotation_slots)
        except (AttributeError, TypeError, ValueError):
            slots = 0
        with_vulns = [a for a in assets if self._vuln_count(a) > 0 and a.get("id") is not None]
        with_vulns.sort(key=lambda a: a.get("riskScore", 0) or 0, reverse=True)
        run_ordinal = 0
        if slots > 0 and len(with_vulns) > cap:
            run_ordinal = self._coverage_rotation_ordinal()
        selected = self._apply_cap_with_rotation(with_vulns, cap, slots, run_ordinal)
        if len(with_vulns) > cap:
            dropped_count = len(with_vulns) - cap
            logger.warning(
                "Asset cap reached: %d assets not enriched (cap=%d)",
                dropped_count,
                cap,
            )
            try:
                # Deliberate fresh session (not the caller's in-flight collection
                # session): this audit row must survive even if the collection
                # transaction rolls back, so it is written and committed
                # independently via a best-effort get_session() rather than
                # joining/reusing the caller's session.
                with get_session() as audit_session:
                    audit_session.add(
                        AgentActionLog(
                            agent=type(self).__name__,
                            domain=self.domain,
                            run_id=str(self._active_run_id),
                            tool="truncation",
                            args_summary=json.dumps(
                                {
                                    "cap": cap,
                                    "dropped_count": dropped_count,
                                    "entity": "rapid7_vuln_asset_cap",
                                    "rotation_slots": max(0, min(slots, cap)),
                                    "run_ordinal": run_ordinal,
                                }
                            ),
                            verdict="allow",
                            status="ok",
                        )
                    )
                    audit_session.commit()
            except Exception:
                logger.exception(
                    "VulnAgent: truncation-counter audit row write failed "
                    "(entity=rapid7_vuln_asset_cap)"
                )
        logger.info(
            "VulnAgent: vuln_queue feed — %d assets total, %d with vulnerabilities, "
            "%d selected after cap=%d (%d skipped by cap; rotation_slots=%d, "
            "run_ordinal=%d)",
            len(assets),
            len(with_vulns),
            len(selected),
            cap,
            max(0, len(with_vulns) - len(selected)),
            max(0, min(slots, cap)),
            run_ordinal,
        )
        return selected

    @staticmethod
    def _severity_band(severity, cvss) -> str:
        """Normalize a Rapid7 severity → canonical band; CVSS fallback.

        The severity string is resolved through the canonical vocabulary
        (:func:`sev.normalize_severity`), which maps Rapid7's ``Critical`` /
        ``Severe`` / ``Moderate`` words to the ``critical`` / ``high`` /
        ``medium`` bands. Rapid7's ``Severe`` is its "high" band — before this
        was routed through the module, a "Severe" finding with no CVSS score
        fell through to the lenient 90-day ``low`` SLA.
        """
        band = sev.normalize_severity(severity)
        if band is not None:
            return band
        if cvss is not None:
            try:
                c = float(cvss)
            except (TypeError, ValueError):
                c = 0.0
            if c >= 9.0:
                return sev.CRITICAL
            if c >= 7.0:
                return sev.HIGH
            if c >= 4.0:
                return sev.MEDIUM
        return sev.LOW

    @staticmethod
    def _cvss_of(vuln: dict):
        """Pull a CVSS score from a Rapid7 asset-vuln row (best-effort)."""
        for key in ("cvssV3Score", "cvssScore", "cvss"):
            val = vuln.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        return None

    @staticmethod
    def _extract_cves(vuln: dict) -> list[str]:
        """Delegates to the shared helper — see ``agents/_cve.py``."""
        return extract_cves(vuln)

    def _fetch_asset_vulns(self, asset_id):
        """Fetch one asset's Rapid7 findings.

        Returns ``(asset_id, resources, exc)`` — ``exc`` is ``None`` on success
        and the swallowed exception on failure. C-1: the third element is NOT
        cosmetic. This used to return ``(asset_id, [])`` on failure, making a
        transient 500 indistinguishable from "Rapid7 reports zero findings for
        this host" — and the FE-9 reconciliation pass in ``_write_vuln_queue``
        then auto-closed that host's ENTIRE open-CVE set as remediated. The
        caller MUST route the failure into a ``ReconcileScope`` so the
        destructive pass never touches an asset whose fetch did not succeed.
        """
        _cb = {"callbacks": self.callbacks}
        try:
            payload = rapid7_asset_vulnerabilities_tool.invoke({"asset_id": asset_id}, config=_cb)
            resources = payload.get("resources", []) if isinstance(payload, dict) else []
            return asset_id, resources or [], None
        except Exception as exc:
            logger.warning("VulnAgent: failed to fetch vulns for asset %s: %s", asset_id, exc)
            return asset_id, [], exc

    def _write_vuln_queue(self, result: CollectionResult | None = None) -> None:
        # GitLab #173 / #188 Bug 3: (resource_id, cve_id, slug, first_seen) for
        # every row written THIS run with the "not yet enriched" placeholder
        # severity (sev.LOW) — consumed by _reconcile_unenriched_severity
        # after _write_vuln_details runs later in the same detail-write phase,
        # so a CVE whose enrichment lands in the SAME collection run gets its
        # real severity resolved immediately instead of sitting on the
        # placeholder until the host happens to be re-upserted (i.e. selected
        # again within the 750-asset cap) on some later run.
        self._unenriched_vuln_rows: list[tuple] = []

        assets = getattr(self, "_last_assets", None)
        if not assets:
            logger.info("VulnAgent: no assets collected — vuln_queue feed skipped")
            return

        selected = self._bounded_assets(assets)
        if not selected:
            return

        with get_session() as session:
            # asset_id -> the canonical r7_asset Resource id (collect() upserted
            # it keyed by hostName/ip — the same name we rebuild here).
            asset_resource_id: dict = {}
            for asset in selected:
                name = asset.get("hostName") or asset.get("ip") or str(asset.get("id", ""))
                res = (
                    session.query(Resource)
                    .filter_by(domain="vuln", type="r7_asset", name=name)
                    .first()
                )
                if res is not None:
                    asset_resource_id[asset.get("id")] = res.id
                else:
                    logger.warning(
                        "VulnAgent: no r7_asset Resource for asset id=%s name=%r — "
                        "skipping its vulns",
                        asset.get("id"),
                        name,
                    )

            fetch_ids = [a.get("id") for a in selected if a.get("id") in asset_resource_id]
            now = datetime.now(UTC)
            written = 0  # CVE rows upserted
            skipped_no_cve = 0  # non-CVE Rapid7 checks (no extractable CVE)
            failed_rows = 0  # rows guarded out so one bad record can't abort the batch
            # Dedupe across vulns/rows on the same asset before touching the DB
            # (the unique key is (resource_id, cve_id) — a CVE seen in two findings
            # on the same host is one queue row).
            seen_keys: set[tuple] = set()
            # Harvest the DISTINCT Rapid7 vuln SLUG ids (the per-finding ``id``,
            # e.g. ``microsoft-asp_net_core-cve-2023-36038``) + the strongest
            # severity seen for each, so the MR7 vuln-detail phase can enrich them
            # (bounded + cached) without a second pass over the assets.
            distinct_slugs: dict[str, str] = {}

            # C-1: partition the fan-out into PROVEN-observed assets and failed
            # ones. Only the observed set may be reconciled below — an asset
            # whose fetch raised has an empty finding list purely because the
            # call failed, and closing its rows on that basis fabricates a
            # remediation that never happened.
            scope = ReconcileScope(label="rapid7 asset")
            with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
                fetch_results = list(pool.map(self._fetch_asset_vulns, fetch_ids))
            fetched: list[tuple] = []
            for asset_id, vulns, fetch_exc in fetch_results:
                if fetch_exc is None:
                    scope.observed(asset_id)
                    fetched.append((asset_id, vulns))
                else:
                    scope.failed(asset_id, fetch_exc)

            # GL-122 fix: the Rapid7 asset-vulnerabilities item (``v`` below) NEVER
            # carries a real ``severity``/CVSS score — those only exist on the
            # separate vuln *definition* endpoint, which VulnCveBridge enriches
            # into ``r7_vulnerabilities`` keyed by the same slug (``r7_vuln_id``).
            # Deriving severity from ``v`` directly (the old bug) meant
            # ``_severity_band`` always fell through to its ``LOW`` default,
            # regardless of the finding's actual CVSS. Bulk-lookup the enrichment
            # rows for every distinct slug seen this run, once, up front.
            all_slugs: set[str] = {
                str(v.get("id")) for _asset_id, vulns in fetched for v in vulns if v.get("id")
            }
            enrichment: dict[str, tuple] = {}
            if all_slugs:
                for r7_vuln_id, enr_severity, enr_cvss in (
                    session.query(
                        R7Vulnerability.r7_vuln_id,
                        R7Vulnerability.severity,
                        R7Vulnerability.cvss_v3_score,
                    )
                    .filter(R7Vulnerability.r7_vuln_id.in_(all_slugs))
                    .all()
                ):
                    enrichment[r7_vuln_id] = (enr_severity, enr_cvss)

            for asset_id, vulns in fetched:
                resource_id = asset_resource_id.get(asset_id)
                if resource_id is None:
                    continue
                for v in vulns:
                    slug = v.get("id")
                    enr = enrichment.get(str(slug)) if slug else None
                    if slug:
                        # distinct_slugs tracks the strongest severity seen per slug
                        # so the MR7 enrichment phase can prioritize its cap; prefer
                        # the real enriched severity when we already have it, else
                        # fall back to the (unreliable) raw payload value — it only
                        # affects enrichment ORDER, never what gets stored below.
                        sev_raw = str((enr[0] if enr else v.get("severity")) or "").strip().lower()
                        prev = distinct_slugs.get(str(slug))
                        if prev is None or sev.rank(sev_raw) > sev.rank(prev):
                            distinct_slugs[str(slug)] = sev_raw
                    cves = self._extract_cves(v)
                    if not cves:
                        # Non-CVE Rapid7 check — vuln_queue is a CVE queue; skip
                        # (counted + logged, never truncated into cve_id).
                        skipped_no_cve += 1
                        continue
                    if enr is not None:
                        band = self._severity_band(enr[0], enr[1])
                    else:
                        # Not yet enriched (enrichment runs on a lag — see
                        # docs/agents/vuln.md) — explicit "not yet enriched"
                        # placeholder, matching prior behavior for this case.
                        band = sev.LOW
                    # GitLab #136: the SLA clock anchors on the finding's
                    # first-seen date (Rapid7's ``since``), NOT on "now" — the
                    # old ``now + window`` recompute reset the deadline on
                    # every daily rescan, masking all SLA breaches.
                    first_seen = self._parse_since(v.get("since"))
                    # One row per (resource_id, cve) — a multi-CVE vuln fans out;
                    # they all share the asset + severity.
                    for cve_id in cves:
                        key = (resource_id, cve_id)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        # Guard each row in its own SAVEPOINT: a single bad
                        # record (e.g. a stray over-length value) only rolls back
                        # that row, never the prior successful rows — the
                        # all-or-nothing rollback is what zeroed the table.
                        try:
                            with session.begin_nested():
                                self._upsert_vuln_row(
                                    session,
                                    resource_id,
                                    cve_id,
                                    band,
                                    first_seen,
                                    now,
                                    enriched=enr is not None,
                                )
                            written += 1
                            if enr is None and slug:
                                self._unenriched_vuln_rows.append(
                                    (resource_id, cve_id, str(slug), first_seen)
                                )
                        except Exception as exc:
                            # NOTE: key stays in seen_keys even though the write
                            # failed. seen_keys doubles as the FE-9 reconciliation
                            # set below (~line 918) — discarding it here would make
                            # a merely-failed-to-write row indistinguishable from a
                            # genuinely-remediated one, so it would get auto-closed
                            # as CLOSED_AUTO even though Rapid7 still reports it as
                            # open. Leaving the key in seen_keys just means this
                            # run doesn't touch that row at all (neither refresh
                            # nor close it) — safe whether or not a prior row for
                            # this key already exists.
                            failed_rows += 1
                            logger.warning(
                                "VulnAgent: skipped bad vuln_queue row "
                                "(resource_id=%s cve_id=%r): %s",
                                resource_id,
                                cve_id,
                                exc,
                            )
            # FE-9 reconciliation: close out rows for assets we actually
            # re-scanned this run whose CVE is no longer reported. Without
            # this, a vuln_queue row lives forever once created — Rapid7
            # remediating a CVE on a host never gets reflected locally, so
            # "Open CVEs" silently counts already-fixed findings. Scoped to
            # only the resource_ids fetched THIS run (fetch_ids' targets) —
            # never touches assets outside the bounded batch, and never
            # touches a human/remediation-set status (only rows still in an
            # OPEN_VULN_STATUSES state are auto-closed; see db/vuln_status.py
            # for why "triage" counts as auto-manageable but "remediated" /
            # "accepted_risk" / "false_positive" do not).
            #
            # C-1: scoped to ``scope.safe_scope`` — the assets whose fetch
            # actually SUCCEEDED — NOT to ``fetch_ids``. Using fetch_ids meant a
            # single transient Rapid7 500/timeout on one asset auto-closed that
            # host's entire open-CVE set (its findings were "missing" only
            # because the call failed), silently dropping the "Open CVEs" count
            # while the run still reported "completed". This mirrors the guard
            # the row-WRITE-failure path above already applies via seen_keys —
            # both say the same thing: never reconcile on the basis of data you
            # did not actually observe.
            scanned_resource_ids = {
                asset_resource_id[aid] for aid in scope.safe_scope if aid in asset_resource_id
            }
            closed = 0
            if scanned_resource_ids:
                stale_rows = (
                    session.query(VulnQueueItem)
                    .filter(
                        VulnQueueItem.resource_id.in_(scanned_resource_ids),
                        VulnQueueItem.status.in_(OPEN_VULN_STATUSES),
                    )
                    .all()
                )
                for row in stale_rows:
                    if (row.resource_id, row.cve_id) not in seen_keys:
                        row.status = CLOSED_AUTO
                        row.last_updated = now
                        closed += 1
            session.commit()
            logger.info(
                "VulnAgent: vuln_queue feed — upserted %d CVE rows, "
                "skipped %d non-CVE findings, %d rows failed and were guarded out, "
                "%d stale row(s) auto-closed as %s, %d asset fetch(es) failed and "
                "were EXCLUDED from reconciliation (%d asset(s) reconciled)",
                written,
                skipped_no_cve,
                failed_rows,
                closed,
                CLOSED_AUTO,
                scope.failed_count,
                len(scanned_resource_ids),
            )
        # C-1: the swallowed per-asset fetch failures must not stay invisible —
        # a run that could not read part of the fleet is "partial", never a
        # clean "completed" with empty errors. Reported through the shared
        # helper so the CollectionRun row carries the same status vocabulary the
        # collect phase's own CollectOutcome mapping produces.
        self._record_partial_errors(result, scope.errors)

        # Stash the distinct slugs (slug -> strongest severity) for the MR7
        # vuln-detail enrichment phase, which runs after this one.
        self._distinct_vuln_slugs = distinct_slugs

    # --- vuln-detail + solutions enrichment (MR7, bounded + cached) --------
    #
    # TRK-058: this class used to carry its own copies of the fetch/row-mapping
    # helpers (_fetch_vuln_detail, _fetch_solution_ids, _fetch_solution,
    # _text_or_html, _bool_or_none, _vuln_detail_row, _solution_row, _to_int,
    # _capped_slugs) — dead code, fully superseded by VulnCveBridge
    # (agents/vuln_cve.py), which reimplements the same logic independently
    # and is the one actually invoked by _write_vuln_details() below. Deleted
    # after confirming no caller anywhere in the codebase referenced the
    # VulnAgent copies.

    def _write_vuln_details(self) -> None:
        """Enrich the distinct queued vuln slugs: upsert r7_vulnerabilities +
        r7_solutions + r7_vuln_solutions links.

        Thin wrapper — delegates to VulnCveBridge so the enrichment logic can be
        tested independently.  _distinct_vuln_slugs must be populated on self by
        _write_vuln_queue before this is called (the detail-write phase ordering
        in run() guarantees this).
        """
        VulnCveBridge(self).write_vuln_details(session_factory=get_session)

    def _reconcile_unenriched_severity(self) -> None:
        """GitLab #173 / #188 Bug 3: resolve the "low" placeholder within the
        SAME collection run, for whatever ``_write_vuln_details`` just enriched.

        Root cause: ``_write_vuln_queue`` writes a ``sev.LOW`` placeholder for
        any CVE whose Rapid7 vuln slug has no ``r7_vulnerabilities`` row YET
        (enrichment runs on a lag by construction — see the "GL-122 fix" note
        above). ``_write_vuln_details`` then enriches those very slugs later in
        the SAME run, but never revisits the ``vuln_queue`` rows it was fed —
        so, before this method existed, a freshly-enriched severity only ever
        reached ``vuln_queue`` on a LATER run, and only if the host was
        re-upserted (selected again inside the 750-asset cap in
        ``_bounded_assets``). A host that fell out of the cap on the next scan
        could carry an incorrect "low" severity indefinitely. (The coverage-
        rotation slice in ``_apply_cap_with_rotation`` now bounds that
        "indefinitely" — every below-the-cap host is re-selected, and thus
        re-upserted, at least once every ``ceil(R / slots)`` runs.)

        This re-resolves severity, in-run, for every (resource_id, cve_id,
        slug) pair ``_write_vuln_queue`` stashed on
        ``self._unenriched_vuln_rows`` — but only for slugs that actually
        gained an ``r7_vulnerabilities`` row this run. Enrichment itself is
        separately bounded (``rapid7_vuln_detail_cap``, default 600, via
        ``VulnCveBridge._capped_slugs``), so a slug beyond THAT cap still
        won't resolve until a later run enriches it — this closes the
        "same-run" gap, not the enrichment queue's own capacity limit.
        Reuses ``_upsert_vuln_row``'s existing-row path (``enriched=True``)
        so severity-tightening / SLA-recompute behavior is identical to a
        real re-upsert, just without waiting for one.
        """
        rows = getattr(self, "_unenriched_vuln_rows", None) or []
        if not rows:
            return

        slugs = {slug for (_resource_id, _cve_id, slug, _first_seen) in rows}
        with get_session() as session:
            enrichment: dict[str, tuple] = {
                r7_vuln_id: (enr_severity, enr_cvss)
                for r7_vuln_id, enr_severity, enr_cvss in session.query(
                    R7Vulnerability.r7_vuln_id,
                    R7Vulnerability.severity,
                    R7Vulnerability.cvss_v3_score,
                )
                .filter(R7Vulnerability.r7_vuln_id.in_(slugs))
                .all()
            }
            if not enrichment:
                return

            now = datetime.now(UTC)
            reconciled = 0
            for resource_id, cve_id, slug, first_seen in rows:
                enr = enrichment.get(slug)
                if enr is None:
                    continue  # still not enriched this run — capped out, try next run
                band = self._severity_band(enr[0], enr[1])
                try:
                    with session.begin_nested():
                        self._upsert_vuln_row(
                            session, resource_id, cve_id, band, first_seen, now, enriched=True
                        )
                    reconciled += 1
                except Exception as exc:
                    logger.warning(
                        "VulnAgent: severity reconciliation failed "
                        "(resource_id=%s cve_id=%r slug=%r): %s",
                        resource_id,
                        cve_id,
                        slug,
                        exc,
                    )
            session.commit()
            if reconciled:
                logger.info(
                    "VulnAgent: reconciled %d vuln_queue row(s) from placeholder "
                    "'low' to their real enriched severity within this run",
                    reconciled,
                )

    @staticmethod
    def _parse_since(raw) -> datetime | None:
        """Parse Rapid7's per-finding ``since`` first-seen timestamp.

        Rapid7 emits ISO-8601 with a trailing ``Z`` (e.g.
        ``2026-01-04T15:44:57.011Z``). Returns an aware UTC datetime, or None
        when the field is absent/unparseable (callers fall back to ``now`` for
        brand-new rows — never to resetting an existing deadline).
        """
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    def _upsert_vuln_row(
        self, session, resource_id, cve_id, severity, first_seen, now, enriched=True
    ) -> None:
        """Upsert by (resource_id, cve_id). Preserve an existing row's status.

        GitLab #136 contract:

        * NEW row: ``sla_due = first_seen + window(severity)`` where
          ``first_seen`` is Rapid7's ``since`` (fallback: ``now``). The old
          ``now + window`` anchor over-granted the full window to findings
          that had already been open for days upstream.
        * EXISTING row: ``sla_due`` is IMMUTABLE — a rescan re-confirming the
          finding refreshes ``last_updated`` only. The single exception is a
          severity-band TIGHTENING (e.g. the not-yet-enriched "low"
          placeholder upgrading to "critical" once enrichment lands): the
          deadline is recomputed from the first-seen anchor with the stricter
          window, and even then only ever moves EARLIER, never later.
        * ``enriched=False`` (no r7_vulnerabilities row yet → "low" is a
          placeholder, not a measurement) must not DOWNGRADE a previously
          enriched severity — otherwise a rescan racing the enrichment lag
          flapped critical rows back to low daily.
        """
        existing = (
            session.query(VulnQueueItem).filter_by(resource_id=resource_id, cve_id=cve_id).first()
        )
        if existing is None:
            anchor = first_seen or now
            session.add(
                VulnQueueItem(
                    resource_id=resource_id,
                    cve_id=cve_id,
                    severity=severity,
                    sla_due=anchor + timedelta(days=sev.sla_days(severity)),
                    status="open",
                    last_updated=now,
                )
            )
            return

        # Do NOT overwrite status — triage/remediated/accepted is human state.
        old_severity = existing.severity
        if enriched or sev.rank(severity) > sev.rank(old_severity):
            existing.severity = severity
        existing.last_updated = now

        if existing.sla_due is None:
            # Legacy row with no deadline at all — set one from the best
            # available anchor.
            existing.sla_due = (first_seen or now) + timedelta(days=sev.sla_days(existing.severity))
        elif sev.rank(existing.severity) > sev.rank(old_severity):
            # Severity tightened — recompute from the first-seen anchor
            # (derive it from the old deadline when the payload carries no
            # ``since``), but never EXTEND an existing deadline.
            due = (
                existing.sla_due
                if existing.sla_due.tzinfo
                else existing.sla_due.replace(tzinfo=UTC)
            )
            anchor = first_seen or (due - timedelta(days=sev.sla_days(old_severity)))
            candidate = anchor + timedelta(days=sev.sla_days(existing.severity))
            existing.sla_due = min(due, candidate)

    # ── _write_graph_edges (MR8) — DELETED (P5). ─────────────────────────────
    #
    # WHAT IT WROTE: two edge families into ``resource_relationships`` and
    # nothing else — it opened its own session, ran three read-only joins, and
    # made two ``emit_edges_batch`` calls:
    #
    #   VULNERABLE_TO (r7_asset -> r7_vulnerability), one per (asset, vuln
    #     slug), resolved vuln_queue.resource_id -> r7_vuln_cves ->
    #     r7_vulnerabilities.resource_id, deduped keeping the worst cvss_v3,
    #     carrying the canonical 4-key VulnerableToProps superset
    #     {cve_id, severity, exploits, malware_kits}.
    #   PATCHED_BY (r7_vulnerability -> r7_solution), one per (vuln, solution)
    #     from the r7_vuln_solutions bridge, carrying {solution_type}.
    #
    # It wrote no detail rows and no ``resources`` rows: every table it touched
    # (r7_assets, vuln_queue, r7_vulnerabilities, r7_vuln_cves, r7_solutions,
    # r7_vuln_solutions) it only READ. The four surviving detail writers in
    # ``_detail_writers()`` are untouched; this one is simply no longer in the
    # list.
    #
    # WHY IT IS GONE: the store it wrote into is being dropped (P5 of
    # docs/decisions/2026-08-11-graph-first-architecture.md). Per-type verdict
    # for both types as emitted HERE: **retired domain, zero rows ever**.
    # VulnAgent is the Rapid7 (InsightVM) collector; Rapid7 is not deployed on
    # this estate, the collector is retired, and the T3 drop audit measured
    # zero live rows from ``source='vuln_collector'``. Every r7_* table this
    # method joined is empty, so both loops have always produced an empty edge
    # list and both ``emit_edges_batch`` calls have always been skipped by the
    # ``if edges:`` guards. Deleting it loses no capability that ever existed.
    #
    # NEITHER TYPE IS A §3.1 CONTAINMENT REFUSAL. asset->vulnerability and
    # vulnerability->solution each connect two independently-referrable
    # entities with their own ``resources`` rows (populated by
    # ``_write_vuln_details``, which survives). They are deleted because their
    # store is going away and this emitter never populated it.
    #
    # WHERE THE FACTS LIVE: exactly where this method read them from.
    #   "what is host X vulnerable to?"  -> vuln_queue (resource_id, cve_id,
    #     severity, status) joined to r7_vulnerabilities — the same indexed
    #     join this method ran, minus the edge write.
    #   "what patches vulnerability Y?"  -> r7_vuln_solutions -> r7_solutions.
    # Both are FK lookups over tables the collector already writes; the edge
    # was a second copy of a fact the relational schema already held, which is
    # why P4 already re-pointed the licensing/reporting readers off it.
    #
    # If Rapid7 is ever deployed and these are wanted as graph edges, they come
    # back as COLLECTOR DECLARATIONS (``spec.emits_edges``) over those same
    # tables, materialised into ``graph_edges`` by ``graph_engine`` — never as
    # a re-derivation into the dropped store.
    #
    # Cross-source IS_SAME_AS was already NOT emitted here before this deletion
    # — HostReconcileAgent remains the sole writer of IS_SAME_AS (Task 4.6).
