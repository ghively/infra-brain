"""VulnCveBridge — CVE-detail and solution enrichment extracted from VulnAgent.

Encapsulates the bounded, cached, concurrent enrichment pass that populates:

- r7_vulnerabilities  (one row per distinct Rapid7 vuln slug)
- r7_vuln_cves        (CVE-bridge: one row per slug<->CVE)
- r7_solutions        (one row per solution fetched)
- r7_vuln_solutions   (link: slug <-> solution id)

Extracted from VulnAgent._write_vuln_details so it can be tested independently
and reused by future agents without dragging in the full VulnAgent.

Public API
----------
VulnCveBridge(agent)
    Constructs the bridge bound to a BaseAgent instance (for _upsert_detail,
    _upsert_resource, callbacks, and the shared static helpers).

bridge.write_vuln_details()
    Full enrichment pass — call with the agent's ``_distinct_vuln_slugs`` dict
    already populated on the agent instance (set by _write_vuln_queue).

bridge.write_solutions(session, slug, sol_ids, solution_cache, counters)
    Write the solutions for a single slug — exposed for testing.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from infra_brain.agents._cve import extract_cves
from infra_brain.db.models import (
    R7Solution,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
)
from infra_brain.db.session import get_session
from infra_brain.tools.rapid7 import (
    rapid7_solution_tool,
    rapid7_vuln_detail_tool,
    rapid7_vuln_solutions_tool,
)

if TYPE_CHECKING:
    from infra_brain.agents.base import BaseAgent

logger = logging.getLogger(__name__)

# Bounded fan-out workers — matches VulnAgent._FETCH_WORKERS.
_FETCH_WORKERS = 8

# Severity priority for enrichment cap ordering (mirrors VulnAgent._SEVERITY_RANK).
_SEVERITY_RANK = {"critical": 4, "severe": 3, "high": 3, "moderate": 2, "medium": 2, "low": 1}


class VulnCveBridge:
    """Bounded CVE/solution enrichment helper, bound to a VulnAgent instance."""

    def __init__(self, agent: "BaseAgent") -> None:
        self._agent = agent

    # ------------------------------------------------------------------
    # Public entry-points
    # ------------------------------------------------------------------

    def write_vuln_details(self, session_factory=None) -> None:
        """Enrich distinct queued vuln slugs: r7_vulnerabilities + r7_solutions + links.

        Reads ``self._agent._distinct_vuln_slugs`` (slug → strongest severity)
        populated by _write_vuln_queue.  Bounded (cap + severity priority),
        cached (a solution fetched once is not re-fetched), and concurrent
        (bounded thread pool).  Each vuln/solution is guarded in its own
        SAVEPOINT so one bad record cannot abort the batch.

        Identical behaviour to the original VulnAgent._write_vuln_details.

        ``session_factory`` is an optional callable returning a context manager
        that yields a SQLAlchemy session.  Defaults to ``get_session`` from
        ``infra_brain.db.session``.  Callers can inject a patched factory for
        testing without needing to patch this module's import directly.
        """
        if session_factory is None:
            session_factory = get_session

        slugs = self._capped_slugs()
        if not slugs:
            return

        solution_cache: dict[str, dict] = {}
        vulns_written = 0
        solutions_written = 0
        links_written = 0
        cve_links_written = 0
        failed = 0

        with session_factory() as session:
            # Phase A: fetch all vuln details concurrently (read-only, no DB).
            with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
                detail_pairs = list(zip(slugs, pool.map(self._fetch_vuln_detail, slugs)))

            for slug, detail in detail_pairs:
                if not detail:
                    continue
                try:
                    with session.begin_nested():
                        self._agent._upsert_detail(
                            session,
                            R7Vulnerability,
                            self._vuln_detail_row(slug, detail),
                            ["r7_vuln_id"],
                        )
                        # Upsert a canonical Resource row for this vulnerability so it
                        # can participate as a proper knowledge-graph node.
                        vuln_resource = self._agent._upsert_resource(
                            session,
                            {
                                "name": slug,
                                "type": "r7_vulnerability",
                                "data": {
                                    "title": detail.get("title"),
                                    "severity": detail.get("severity"),
                                },
                            },
                        )
                        # Link back: write the resource_id onto the R7Vulnerability row.
                        vrow = session.query(R7Vulnerability).filter_by(r7_vuln_id=slug).first()
                        if vrow is not None:
                            vrow.resource_id = vuln_resource.id
                    vulns_written += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "VulnCveBridge: skipped bad r7_vulnerability row (slug=%r): %s",
                        slug,
                        exc,
                    )
                    continue

                # CVE bridge: explode this vuln's CVE list into r7_vuln_cves
                # (one row per slug<->CVE) so vuln_queue.cve_id can join through
                # to r7_vulnerabilities. CVEs come from the detail payload — no
                # extra API call. Delete-then-reinsert per slug keeps the bridge
                # in sync when CVEs drop from a re-fetched payload.
                try:
                    with session.begin_nested():
                        session.query(R7VulnCve).filter_by(r7_vuln_id=slug).delete(
                            synchronize_session=False
                        )
                        for cve_id in self._extract_cves(detail):
                            session.add(R7VulnCve(r7_vuln_id=slug, cve_id=cve_id))
                            cve_links_written += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "VulnCveBridge: skipped r7_vuln_cves bridge for slug=%r: %s",
                        slug,
                        exc,
                    )

                # Solutions for this vuln (fetched and cached).
                # L-3: distinguish "fetch failed" from "fetch succeeded, zero
                # solutions" -- only a SUCCESSFUL fetch may write a concrete
                # fix_available value. `_vuln_detail_row` no longer resets
                # fix_available on upsert (see below), so a failed fetch here
                # leaves whatever value was already stored untouched instead
                # of erasing a known True.
                sol_ids, solutions_fetch_ok = self._fetch_solution_ids(slug)
                if sol_ids:
                    vrow = session.query(R7Vulnerability).filter_by(r7_vuln_id=slug).first()
                    if vrow is not None:
                        vrow.fix_available = True
                elif solutions_fetch_ok:
                    # Confirmed (not merely failed-to-check): this run found
                    # no solutions for this vuln.
                    vrow = session.query(R7Vulnerability).filter_by(r7_vuln_id=slug).first()
                    if vrow is not None:
                        vrow.fix_available = False
                counters = {"solutions": 0, "links": 0, "failed": 0}
                self.write_solutions(session, slug, sol_ids, solution_cache, counters)
                solutions_written += counters["solutions"]
                links_written += counters["links"]
                failed += counters["failed"]

            session.commit()

        logger.info(
            "VulnCveBridge: vuln-detail enrichment — upserted %d vulns, %d solutions "
            "(%d unique fetched), %d links, %d cve-bridge rows, %d records guarded out",
            vulns_written,
            solutions_written,
            len(solution_cache),
            links_written,
            cve_links_written,
            failed,
        )

    def write_solutions(
        self,
        session,
        slug: str,
        sol_ids: list,
        solution_cache: dict[str, dict],
        counters: dict,
    ) -> None:
        """Write r7_solutions + r7_vuln_solutions link rows for one vuln slug.

        ``solution_cache`` is a shared run-level dict (str→dict) — solutions
        already fetched for another slug in this run are reused, not re-fetched.
        ``counters`` is a mutable dict with keys ``solutions``, ``links``,
        ``failed`` — incremented in-place so the caller can aggregate totals.
        """
        for sol_id in sol_ids:
            sol_id = str(sol_id)
            if sol_id not in solution_cache:
                solution_cache[sol_id] = self._fetch_solution(sol_id)
            sol = solution_cache[sol_id]
            if sol:
                try:
                    with session.begin_nested():
                        self._agent._upsert_detail(
                            session,
                            R7Solution,
                            self._solution_row(sol_id, sol),
                            ["r7_solution_id"],
                        )
                        # Upsert a canonical Resource row for this solution.
                        sol_resource = self._agent._upsert_resource(
                            session,
                            {
                                "name": sol_id,
                                "type": "r7_solution",
                                "data": {
                                    "summary": self._text_or_html(sol.get("summary")),
                                    "solution_type": sol.get("type"),
                                },
                            },
                        )
                        # Link back: write the resource_id onto the R7Solution row.
                        srow = session.query(R7Solution).filter_by(r7_solution_id=sol_id).first()
                        if srow is not None:
                            srow.resource_id = sol_resource.id
                    counters["solutions"] += 1
                except Exception as exc:
                    counters["failed"] += 1
                    logger.warning(
                        "VulnCveBridge: skipped bad r7_solution row (id=%r): %s",
                        sol_id,
                        exc,
                    )

            # Link row (slug <-> solution id), upsert on natural key.
            try:
                with session.begin_nested():
                    self._agent._upsert_detail(
                        session,
                        R7VulnSolution,
                        {"r7_vuln_id": slug, "r7_solution_id": sol_id},
                        ["r7_vuln_id", "r7_solution_id"],
                    )
                counters["links"] += 1
            except Exception as exc:
                counters["failed"] += 1
                logger.warning(
                    "VulnCveBridge: skipped bad r7_vuln_solution link (vuln=%r solution=%r): %s",
                    slug,
                    sol_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Private helpers (mirrors / lifted from VulnAgent)
    # ------------------------------------------------------------------

    def _capped_slugs(self) -> list[str]:
        """Top-N distinct slugs by severity (critical/severe first), capped. Logged."""
        slugs: dict[str, str] = getattr(self._agent, "_distinct_vuln_slugs", None) or {}
        cap = getattr(self._agent.settings, "rapid7_vuln_detail_cap", 600) or 600
        ordered = sorted(slugs.items(), key=lambda kv: _SEVERITY_RANK.get(kv[1], 0), reverse=True)
        selected = [slug for slug, _sev in ordered[:cap]]
        skipped = max(0, len(slugs) - len(selected))
        if skipped:
            logger.warning(
                "VulnCveBridge: enrichment cap reached: %d vulns not enriched (cap=%d)",
                skipped,
                cap,
            )
        logger.info(
            "VulnCveBridge: vuln-detail enrichment — %d distinct vulns found, "
            "%d to enrich after cap=%d (%d skipped by cap)",
            len(slugs),
            len(selected),
            cap,
            skipped,
        )
        return selected

    def _fetch_vuln_detail(self, slug: str) -> dict:
        _cb = {"callbacks": self._agent.callbacks}
        try:
            payload = rapid7_vuln_detail_tool.invoke({"vuln_id": slug}, config=_cb)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("VulnCveBridge: failed to fetch vuln detail for %s: %s", slug, exc)
            return {}

    def _fetch_solution_ids(self, slug: str) -> tuple[list, bool]:
        """Returns ``(ids, ok)``. L-3: ``ok`` distinguishes a genuinely empty
        result (fetch succeeded, zero solutions) from a swallowed failure —
        the caller must not treat the two the same way, since only the
        former is grounds to write a concrete ``fix_available=False``.
        """
        _cb = {"callbacks": self._agent.callbacks}
        try:
            ids = rapid7_vuln_solutions_tool.invoke({"vuln_id": slug}, config=_cb)
            return (list(ids) if ids else []), True
        except Exception as exc:
            logger.warning("VulnCveBridge: failed to fetch solution ids for %s: %s", slug, exc)
            return [], False

    def _fetch_solution(self, solution_id: str) -> dict:
        _cb = {"callbacks": self._agent.callbacks}
        try:
            payload = rapid7_solution_tool.invoke({"solution_id": solution_id}, config=_cb)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("VulnCveBridge: failed to fetch solution %s: %s", solution_id, exc)
            return {}

    # --- Row mappers (identical logic to VulnAgent; static-compatible) ------

    @staticmethod
    def _text_or_html(field) -> str | None:
        """Rapid7 ``summary``/``steps`` come as {text, html}. Prefer text over html."""
        if isinstance(field, dict):
            return field.get("text") or field.get("html")
        if isinstance(field, str):
            return field
        return None

    @staticmethod
    def _bool_or_none(value):
        return bool(value) if value is not None else None

    @staticmethod
    def _to_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value) -> int:
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _trunc(value, length: int):
        if value is None:
            return None
        s = str(value)
        return s[:length] if len(s) > length else s

    @staticmethod
    def _parse_dt(value):
        if not value:
            return None
        from datetime import datetime as _dt

        try:
            return _dt.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_cves(vuln: dict) -> list[str]:
        """Delegates to the shared helper — see ``agents/_cve.py``."""
        return extract_cves(vuln)

    def _vuln_detail_row(self, slug: str, detail: dict) -> dict:
        """Map a Rapid7 vulnerability-detail payload → an r7_vulnerabilities row."""
        cvss = detail.get("cvss") if isinstance(detail.get("cvss"), dict) else {}
        v3 = cvss.get("v3") if isinstance(cvss.get("v3"), dict) else {}
        v2 = cvss.get("v2") if isinstance(cvss.get("v2"), dict) else {}
        pci = detail.get("pci") if isinstance(detail.get("pci"), dict) else {}
        pci_status = pci.get("status")
        if isinstance(pci.get("fail"), bool):
            pci_fail = pci["fail"]
        elif isinstance(pci_status, str):
            pci_fail = pci_status.strip().lower() == "fail"
        else:
            pci_fail = None
        extras = {
            k: detail.get(k)
            for k in ("exploits", "malwareKits", "added", "modified", "description", "links")
            if detail.get(k) is not None
        }
        return {
            "r7_vuln_id": slug,
            "title": detail.get("title"),
            "severity": self._trunc(detail.get("severity"), 16),
            "cvss_v3_score": self._to_float(v3.get("score")),
            "cvss_v3_vector": self._trunc(v3.get("vector"), 256),
            "cvss_v2_score": self._to_float(v2.get("score")),
            "risk_score": self._to_float(detail.get("riskScore")),
            "exploits": self._to_int(detail.get("exploits")),
            "malware_kits": self._to_int(detail.get("malwareKits")),
            "denial_of_service": self._bool_or_none(detail.get("denialOfService")),
            # L-3: intentionally NOT set here. `_upsert_detail` (base.py)
            # setattr's every key present in this dict onto an EXISTING row,
            # so putting a hardcoded None here reset a known-good
            # fix_available=True to None on every re-enrichment BEFORE the
            # solutions fetch below even ran -- indistinguishable from an
            # actual "no fix" answer once that fetch then failed too. Leaving
            # the key out means `_upsert_detail` never touches this column on
            # UPDATE; the solutions-fetch block below is the sole writer, and
            # only when it actually observed a result (success, even if
            # empty) — see `_fetch_solution_ids`'s ``ok`` flag. A brand-new
            # row simply gets the model's column default (nullable/None)
            # until solutions are checked, same effective behavior as before.
            "pci_status": self._trunc(pci_status, 16),
            "pci_fail": pci_fail,
            "published": self._parse_dt(detail.get("published")),
            "categories": detail.get("categories") or [],
            "cves": self._extract_cves(detail),
            "details": extras or None,
        }

    @staticmethod
    def _solution_row(solution_id: str, sol: dict) -> dict:
        steps = VulnCveBridge._text_or_html(sol.get("steps"))
        extras = {
            k: sol.get(k)
            for k in ("additionalInformation", "appliesTo", "links")
            if sol.get(k) is not None
        }
        return {
            "r7_solution_id": solution_id,
            "summary": VulnCveBridge._text_or_html(sol.get("summary")),
            "steps": steps,
            "solution_type": VulnCveBridge._trunc(sol.get("type"), 64),
            "estimate": VulnCveBridge._trunc(sol.get("estimate"), 64),
            "fix_available": bool(steps) if steps is not None else None,
            "details": extras or None,
        }
