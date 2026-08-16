"""Read-only SQL query tools for the Infra Brain chat agent.

Each tool runs a read-only SQL query against the primary database and returns a
human-readable text table suitable for inclusion in an LLM response. These tools
use the core SQLAlchemy engine directly (no pandas, no Streamlit dependency) so
they work identically under the FastAPI dashboard and in tests/CLI.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import text

from infra_brain import graph_kg
from infra_brain.db.session import get_engine, get_session
from infra_brain.tools.host_purpose_map_mr import open_host_purpose_map_mr

# ---------------------------------------------------------------------------
# Tool result truncation (shared by agent.py tools too)
# ---------------------------------------------------------------------------

# ~12k was the original arbitrary figure. Measured against the real estate: a
# single ordinary homelab host's identity-merged neighbourhood serialises to
# ~15.5k chars (node_a: 33 nodes / 33 edges), so the most common question this
# lane exists to answer — "what is connected to X" — was being trimmed for
# every host. 24k (~6k tokens) fits that whole and still bounds the pathological
# case; the trimming path below remains the guard, it is simply no longer the
# normal path. This is the INTERACTIVE lane: the batch ReAct loop has its own,
# tighter budget (agents/llm_base.py::_tool_result_max_chars), because there a
# result is re-read on every subsequent turn and compounds.
_DEFAULT_MAX_CHARS = 24000


def _truncate_tool_result(result: Any, max_chars: int = _DEFAULT_MAX_CHARS) -> Any:
    """Truncate oversized tool results to avoid context bloat.

    - str results are truncated to *max_chars* characters with a trailing note.
    - list results: items are included until the budget is exhausted, then a
      summary envelope replaces the full list.
    - dict results: partial JSON up to *max_chars* with a truncation note.
    - Everything else is returned unchanged.
    """
    if isinstance(result, str):
        if len(result) <= max_chars:
            return result
        return (
            result[:max_chars]
            + f"\n\n[... truncated — result exceeded {max_chars} chars. Narrow your query for full output.]"
        )
    if isinstance(result, (dict, list)):
        serialized = json.dumps(result, default=str)
        if len(serialized) <= max_chars:
            return result
        if isinstance(result, list):
            truncated = []
            size = 0
            for item in result:
                item_str = json.dumps(item, default=str)
                if size + len(item_str) > max_chars:
                    break
                truncated.append(item)
                size += len(item_str)
            return {
                "_truncated": True,
                "count": len(result),
                "shown": len(truncated),
                "data": truncated,
                "hint": "Result truncated — add a WHERE or LIMIT to narrow the query",
            }
        # NEVER slice-then-parse. The original implementation did
        # ``json.loads(serialized[:max_chars])``, which cuts serialized JSON at
        # an arbitrary character and hands the fragment to the parser — that
        # raises ``JSONDecodeError: Unterminated string ...`` for essentially
        # every oversized dict, so the model received an exception instead of a
        # degraded answer. Latent since it was written; it first fired when the
        # identity-merged knowledge-graph payload crossed 12k chars, and the
        # model reported it to the operator as "a data issue specific to
        # node_a" — a truncation bug wearing the costume of database corruption.
        #
        # TRIM THE LISTS, DON'T DROP THE KEYS. Whole-key dropping was the first
        # fix and it was still wrong: on a neighbourhood payload it kept the
        # verbose ``nodes`` list and dropped ``edges`` — for "what is connected
        # to X", the edges ARE the answer. Keeping every key with fewer rows
        # preserves the shape the model reasons over, and each trimmed list
        # says how many of how many it is showing, so a partial answer is
        # visibly partial instead of quietly wrong.
        scalars: dict[str, Any] = {}
        lists: dict[str, list] = {}
        for key, value in result.items():
            if isinstance(value, list):
                lists[key] = value
            else:
                scalars[key] = value

        budget = max_chars - len(json.dumps(scalars, default=str))
        if budget <= 0 or not lists:
            # Even the non-list keys do not fit, or there is nothing to trim.
            return {
                "_truncated": True,
                "hint": "Result too large to display — ask about one entity.",
                "keys": sorted(result),
            }

        # Share the remaining budget evenly across the list-valued keys so no
        # single long list can starve the others (the failure above).
        per_list = max(1, budget // len(lists))
        trimmed: dict[str, Any] = {}
        counts: dict[str, str] = {}
        for key, value in lists.items():
            kept: list = []
            used = 0
            for item in value:
                item_len = len(json.dumps(item, default=str)) + 1
                if used + item_len > per_list:
                    break
                kept.append(item)
                used += item_len
            trimmed[key] = kept
            if len(kept) < len(value):
                counts[key] = f"showing {len(kept)} of {len(value)}"

        return {
            "_truncated": True,
            "hint": (
                "Lists were trimmed to fit the context budget — reduce depth or "
                "ask about one entity for the complete set."
            ),
            "trimmed": counts,
            **scalars,
            **trimmed,
        }
    return result


def _rows(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    """Execute *sql* and return rows as a list of plain dicts."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(m) for m in result.mappings()]


def _fmt(rows: list[dict[str, Any]]) -> str:
    """Render *rows* as a fixed-width text table (column-aligned)."""
    if not rows:
        return ""
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    body = "\n".join("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) for r in rows)
    return f"{header}\n{body}"


@tool
def query_resources(
    domain: str = "", resource_type: str = "", limit: int = 20, format: str = "text"
) -> str:
    """
    Query infrastructure resources from the database.

    Args:
        domain: filter by domain (linux, cloud, k8s, net, windows, octopus, etc.) — leave blank for all
        resource_type: filter by resource type (host, vm, pod, switch, etc.) — leave blank for all
        limit: max number of rows to return (default 20, max 100)
        format: output format — 'text' (default) for a human-readable table, 'json' for a JSON list
    """
    limit = min(limit, 100)
    # GitLab #206: plain `ORDER BY last_seen DESC` with no domain filter lets
    # whichever domain sweeps most often (netdiscovery, every 15 min) fill
    # every slot in the result, swamping fleet diversity (200/200 rows from
    # one domain). The ORDER BY's first key counts how many rows in the SAME
    # domain are more recent than this one (0 = newest in its domain);
    # ordering by that first takes the newest row from every domain before
    # taking a second row from any domain — round-robin-by-recency across
    # domains. It's a plain (unselected) ORDER BY expression, not a SELECT-
    # list column, so it never leaks into the returned rows. When `domain` is
    # already filtered to one value this is a no-op: it's identical to plain
    # last_seen order within a single domain.
    sql = """
        SELECT name AS hostname, domain, type AS resource_type, zone, last_seen AS last_seen_at
        FROM resources
        WHERE (:domain = '' OR domain = :domain)
          AND (:resource_type = '' OR type = :resource_type)
        ORDER BY
            (SELECT COUNT(*) FROM resources r2
             WHERE r2.domain = resources.domain AND r2.last_seen > resources.last_seen),
            last_seen DESC
        LIMIT :limit
    """
    rows = _rows(sql, {"domain": domain, "resource_type": resource_type, "limit": limit})
    if not rows:
        return "No resources found matching those filters."
    if format == "json":
        return _truncate_tool_result(json.dumps(rows, default=str))
    return _truncate_tool_result(_fmt(rows))


@tool
def query_drift_events(
    hours: int = 24, status: str = "open", domain: str = "", format: str = "text"
) -> str:
    """
    Query drift events detected in the last N hours.

    Args:
        hours: look back window in hours (default 24)
        status: open | acknowledged | resolved (default open)
        domain: filter by domain — leave blank for all
        format: output format — 'text' (default) for a human-readable table, 'json' for a JSON list
    """
    # GitLab #163/#164: detected_at is now the immutable FIRST-observation stamp,
    # so both the look-back window and the ordering use
    # COALESCE(last_seen_at, detected_at). Filtering on detected_at alone would
    # drop a finding re-observed five minutes ago out of "drift in the last 24h"
    # purely because it happened to be first seen last month.
    #
    # NOTE: keep explanatory prose OUT of the SQL string — the sql-execution-check
    # gate (tests/test_dashboard_sql_columns.py) parses every bare identifier in
    # these literals and reads `-- ...` comment words as column names.
    sql = """
        SELECT de.id, r.domain, r.name AS hostname, de.field AS field_name,
               de.old_value, de.new_value, de.detected_at, de.status,
               jt.jira_key AS jira_ticket
        FROM drift_events de
        LEFT JOIN resources r ON r.id = de.resource_id
        LEFT JOIN jira_tickets jt ON jt.drift_event_id = de.id
        WHERE COALESCE(de.last_seen_at, de.detected_at) >= NOW() - (:hours * INTERVAL '1 hour')
          AND (:status = '' OR de.status = :status)
          AND (:domain = '' OR r.domain = :domain)
        ORDER BY COALESCE(de.last_seen_at, de.detected_at) DESC
        LIMIT 50
    """
    rows = _rows(sql, {"hours": hours, "status": status, "domain": domain})
    if not rows:
        return f"No drift events with status '{status}' in the last {hours} hours."
    if format == "json":
        return _truncate_tool_result(json.dumps(rows, default=str))
    return _truncate_tool_result(_fmt(rows))


@tool
def query_collection_runs(domain: str = "", limit: int = 10, format: str = "text") -> str:
    """
    Query recent collection run history.

    Args:
        domain: filter by domain — leave blank for all
        limit: max rows (default 10)
        format: output format — 'text' (default) for a human-readable table, 'json' for a JSON list
    """
    sql = """
        SELECT domain, trigger_type,
               CASE status WHEN 'completed' THEN 'success'
                           WHEN 'failed' THEN 'failure'
                           WHEN 'in_progress' THEN 'running'
                           WHEN 'retry_exhausted' THEN 'failure'
                           WHEN 'interrupt_pending' THEN 'running'
                           ELSE status END AS status,
               resources_found AS records_collected,
               started_at,
               EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_seconds
        FROM collection_runs
        WHERE (:domain = '' OR domain = :domain)
        ORDER BY started_at DESC
        LIMIT :limit
    """
    rows = _rows(sql, {"domain": domain, "limit": min(limit, 50)})
    if not rows:
        return "No collection runs found."
    if format == "json":
        return _truncate_tool_result(json.dumps(rows, default=str))
    return _truncate_tool_result(_fmt(rows))


@tool
def query_fleet_health(format: str = "text") -> str:
    """Return a fleet health summary: open drift count, open CVEs, EOL count, last scan times.

    Args:
        format: output format — 'text' (default) for a human-readable summary, 'json' for a JSON dict
    """
    sql = """
        SELECT
            (SELECT COUNT(*) FROM drift_events WHERE status = 'open') AS open_drift,
            (SELECT COUNT(*) FROM vuln_queue WHERE status = 'open') AS open_cves,
            (SELECT COUNT(*) FROM eol_registry) AS eol_resources,
            (SELECT MAX(started_at) FROM collection_runs WHERE status = 'completed') AS last_successful_run
    """
    rows = _rows(sql)
    if not rows:
        return "Unable to retrieve fleet health."
    row = rows[0]
    if format == "json":
        return _truncate_tool_result(json.dumps(row, default=str))
    return _truncate_tool_result(
        f"Open drift events: {row['open_drift']}\n"
        f"Open CVEs: {row['open_cves']}\n"
        f"EOL resources: {row['eol_resources']}\n"
        f"Last successful collection: {row['last_successful_run']}"
    )


@tool
def query_linux_detail(hostname: str, info_type: str = "all") -> str:
    """Query Linux host detail: packages, services, users, ports, or crons.

    Args:
        hostname: The Linux host name or resource name to query
        info_type: One of: packages, services, users, ports, crons, all
    """
    # Look up the resource by name
    resource_rows = _rows(
        "SELECT id FROM resources WHERE name = :name AND domain = 'linux' LIMIT 1",
        {"name": hostname},
    )
    if not resource_rows:
        return (
            f"Host '{hostname}' not found. No Linux resource with that name exists in the database."
        )

    resource_id = str(resource_rows[0]["id"])

    # Look up the linux_host record
    host_rows = _rows(
        "SELECT id, distro, kernel, arch FROM linux_hosts WHERE resource_id = :rid LIMIT 1",
        {"rid": resource_id},
    )
    if not host_rows:
        return f"Error: resource '{hostname}' found but no linux_host detail record exists."

    host_id = str(host_rows[0]["id"])
    host = host_rows[0]

    sections: list[str] = [
        f"Host: {hostname}",
        f"Distro: {host.get('distro', 'unknown')}  Kernel: {host.get('kernel', 'unknown')}  "
        f"Arch: {host.get('arch', '?')}",
    ]

    queries: dict[str, tuple[str, str]] = {
        "packages": (
            "SELECT name, version, manager FROM linux_packages WHERE host_id = :hid ORDER BY name LIMIT 50",
            "Packages (top 50)",
        ),
        "services": (
            "SELECT name, state, enabled FROM linux_services WHERE host_id = :hid ORDER BY name LIMIT 50",
            "Services",
        ),
        "users": (
            "SELECT username, shell, sudo FROM linux_users WHERE host_id = :hid ORDER BY username LIMIT 50",
            "Users",
        ),
        "ports": (
            "SELECT port, proto, state, process FROM linux_ports WHERE host_id = :hid ORDER BY port LIMIT 50",
            "Open Ports",
        ),
        "crons": (
            "SELECT schedule, command, owner FROM linux_crons WHERE host_id = :hid ORDER BY owner LIMIT 50",
            "Cron Jobs",
        ),
    }

    selected = list(queries.keys()) if info_type == "all" else [info_type]

    for key in selected:
        if key not in queries:
            sections.append(
                f"Unknown info_type '{key}'. Valid: packages, services, users, ports, crons, all"
            )
            continue
        sql, label = queries[key]
        rows = _rows(sql, {"hid": host_id})
        sections.append(f"\n--- {label} ---")
        if rows:
            sections.append(_fmt(rows))
        else:
            sections.append(f"No {key} found.")

    return _truncate_tool_result("\n".join(sections))


@tool
def query_drift_trend(domain: str = "", days: int = 30) -> str:
    """Query drift event trend over time.

    Args:
        domain: Optional domain filter (e.g. 'linux', 'vsphere')
        days: Number of days to look back (default 30)
    """
    sql = """
        SELECT
            CAST(de.detected_at AS DATE) AS date,
            r.domain AS domain,
            COUNT(*) AS count
        FROM drift_events de
        LEFT JOIN resources r ON r.id = de.resource_id
        WHERE de.detected_at >= NOW() - (:days * INTERVAL '1 day')
          AND (:domain = '' OR r.domain = :domain)
        GROUP BY CAST(de.detected_at AS DATE), r.domain
        ORDER BY CAST(de.detected_at AS DATE)
    """
    rows = _rows(sql, {"days": days, "domain": domain})
    if not rows:
        filter_note = f" for domain '{domain}'" if domain else ""
        return f"No drift events found in the last {days} days{filter_note}."
    return _truncate_tool_result(_fmt(rows))


@tool
def query_compliance(
    host: str | None = None, rule: str | None = None, status: str = "open", limit: int = 20
) -> str:
    """
    Query policy-as-code compliance violations found by ComplianceAgent.

    Args:
        host: filter by host name — leave blank/None for all
        rule: filter by rule id (e.g. 'no-root-ssh') — leave blank/None for all
        status: open | resolved (default open) — pass '' for all statuses
        limit: max number of rows to return (default 20, max 100)
    """
    limit = min(limit, 100)
    sql = """
        SELECT rule, severity, host, detail, status, detected_at
        FROM compliance_violations
        WHERE (:host = '' OR host = :host)
          AND (:rule = '' OR rule = :rule)
          AND (:status = '' OR status = :status)
        ORDER BY detected_at DESC
        LIMIT :limit
    """
    rows = _rows(
        sql,
        {"host": host or "", "rule": rule or "", "status": status, "limit": limit},
    )
    if not rows:
        return "No compliance violations found matching those filters."
    return _truncate_tool_result(_fmt(rows))


# ---------------------------------------------------------------------------
# Knowledge-graph neighbourhood
#
# Graph-first P5: these read ``graph_nodes``/``graph_edges`` (the declarative
# store the collectors' ``etl.spec`` contracts materialise), NOT the retired
# ``resource_relationships`` table. The two are different id spaces and hold
# different things — see infra_brain/graph_kg.py's module docstring — and the
# honest consequence is that this tool can no longer answer containment
# ("what packages are on this host") from edges, because the new store has no
# containment edges by design. The cheap half of that gap — a Linux host's
# systemd units — is filled from the detail tables and LABELLED as facts, so
# the model is never handed a smaller graph and left to infer the host runs
# nothing. Everything richer stays where it already lives:
# ``query_linux_detail``.
# ---------------------------------------------------------------------------

#: How many ambiguous name matches to name before giving up and asking the
#: caller to be more specific.
_KG_MAX_CANDIDATES = 10


def kg_neighborhood_payload(ident: str, depth: int = 1, limit: int = 50) -> dict[str, Any]:
    """Shared implementation behind both chat graph tools.

    Returns a plain dict so the LLM-bound tool in ``chat/agent.py`` can hand it
    back as structured data while the text tool below renders the same numbers
    as a table — one query path, two presentations, so they cannot disagree.

    ``ident`` is anything ``graph_kg.resolve_roots`` accepts: a graph-node UUID,
    a ``resources.id`` UUID (bridged via ``graph_nodes.resource_id``), or a
    name / natural key.

    IDENTITY. One name legitimately resolves to several ``graph_nodes`` rows,
    and the two cases that produces are NOT the same question:

    * rows joined by an active ``SAME_AS`` are ONE machine — this returns the
      UNION of their edges, deduplicated, each edge tagged ``via`` the member
      it came through. Nothing to disambiguate, so nothing is reported as
      ambiguous.
    * rows NOT joined by identity are a genuine collision — the first cluster
      is walked and the rest are reported in ``other_matches`` for the caller
      to choose between, exactly as before.

    The old code collapsed both into "walked the first", which is how "what is
    connected to node_a?" came back as "one connection — an identity link
    between two representations of itself" while the sibling row carried every
    real fact about the host.
    """
    limit = max(1, min(int(limit), 200))
    depth = max(1, min(int(depth), graph_kg.MAX_DEPTH))

    with get_session() as session:
        roots = graph_kg.resolve_roots(session, ident, limit=_KG_MAX_CANDIDATES)
        if not roots:
            return {
                "identifier": ident,
                "found": False,
                "reason": (
                    f"'{ident}' not found in the knowledge graph (graph_nodes). "
                    "Node names come from the collectors — try a host name, a "
                    "service name, a GitLab project, or an IaC file path."
                ),
                "nodes": [],
                "edges": [],
                "facts": {},
            }

        groups = graph_kg.group_by_identity(session, roots)
        root = groups[0][0]
        payload = graph_kg.neighborhood(session, root.id, depth=depth, max_edges=limit)
        payload["identifier"] = ident
        payload["found"] = True
        payload["root"] = graph_kg.node_dict(root)
        others = [n for group in groups[1:] for n in group]
        if others:
            # A collision with NO identity link between the sides — a real
            # ambiguity. Say so and let the caller pick; silently merging
            # unrelated entities would be a worse lie than the one this
            # replaced.
            payload["other_matches"] = [graph_kg.node_dict(r) for r in others]

        # Facts follow identity too: the Ansible-sourced row is typically not
        # resource-backed, so asking from that side used to report no systemd
        # units for a host that plainly has them.
        services = _union_linux_service_facts(session, payload["identity_members"])

    payload["facts"] = {
        # Named so the shape itself says these are not edges.
        "linux_services": services,
        "note": (
            "Containment (packages, systemd units, ports, users, crons) is NOT "
            "in the knowledge graph — those are rows in per-domain detail "
            "tables, not relationships between entities. The systemd units "
            "above come from linux_services. For packages/ports/users/crons "
            "use query_linux_detail."
        ),
    }
    return payload


def _union_linux_service_facts(session: Any, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """systemd units for every resource-backed member of the identity cluster.

    Deduplicated by unit name, first member wins — two rows for the same
    machine describe the same box, so a repeat is a duplicate, not a second
    fact.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in members:
        rid = member.get("resource_id")
        if not rid:
            continue
        for fact in graph_kg.linux_service_facts(session, uuid.UUID(str(rid))):
            if fact["service"] in seen:
                continue
            seen.add(fact["service"])
            out.append(fact)
    return out


@tool
def query_resource_neighborhood(resource_name: str, depth: int = 1, limit: int = 50) -> str:
    """
    Query the knowledge graph around a host, service, GitLab project or IaC file.

    Returns the RELATIONSHIPS between entities — RUNS_ON (service -> host),
    BELONGS_TO / DEFINED_IN (IaC file <-> project), RUNS_IMAGE, and identity
    links — each with the confidence and the method it was derived by.

    IMPORTANT — what this cannot tell you: the knowledge graph holds no
    containment edges. A host's packages, listening ports, users and crons are
    recorded as detail-table facts, not as graph relationships, so they will
    NOT appear as edges here. For a Linux host this tool additionally lists the
    systemd units recorded for it under a clearly-labelled "Facts" section; for
    packages, ports, users or crons use query_linux_detail instead. Do not
    conclude from an empty edge list that nothing runs on a host.

    Args:
        resource_name: what to centre the walk on — a host name, service name,
            GitLab project, IaC file path, or a resource/graph-node UUID
        depth: max hops to traverse (default 1, clamped to 4)
        limit: max edges to return (default 50, max 200)
    """
    payload = kg_neighborhood_payload(resource_name, depth=depth, limit=limit)
    if not payload["found"]:
        return payload["reason"]

    root = payload["root"]
    services = payload["facts"]["linux_services"]
    edges = payload["edges"]

    lines = [
        f"Knowledge graph around '{root['name']}' "
        f"({root['type']}, natural key {root['natural_key']}), depth={payload['depth']}:",
        "",
    ]
    if payload.get("identity_merged"):
        members = ", ".join(
            f"{m['name']} ({m['type']}, source {m['source']})" for m in payload["identity_members"]
        )
        lines.append(
            f"IDENTITY: '{resource_name}' is ONE machine recorded by "
            f"{len(payload['identity_members'])} sources — {members} — linked by an active "
            "SAME_AS. Everything below is the UNION across those rows; the 'via' column "
            "says which row each relationship came from. The identity hop is free: "
            f"depth={payload['depth']} still means {payload['depth']} real hop(s)."
        )
        lines.append("")
    if payload.get("identity_vetoed"):
        vetoed = ", ".join(f"{m['name']} ({m['type']})" for m in payload["identity_vetoed"])
        lines.append(
            f"NOT merged: {vetoed} — an operator recorded an explicit NOT_SAME_AS veto "
            "against this machine. Treated as separate entities on purpose."
        )
        lines.append("")
    if payload.get("other_matches"):
        others = ", ".join(f"{m['name']} ({m['type']})" for m in payload["other_matches"])
        lines.append(
            f"AMBIGUOUS: '{resource_name}' also matches {others}, with NO identity link "
            "to the entity below — these are different things that share a name, not "
            "one machine. Walked the first; ask by node type or UUID to see another."
        )
        lines.append("")

    lines.append("Nodes:")
    lines.append(_fmt(payload["nodes"]) if payload["nodes"] else "(none)")
    lines.append("")
    lines.append("Edges (relationships):")
    if edges:
        lines.append(_fmt(edges))
        if payload["truncated"]:
            lines.append(
                f"(showing {len(payload['nodes'])} of {payload['node_total']} nodes and "
                f"{len(edges)} of {payload['edge_total']} edges — raise `limit` or narrow the query)"
            )
    else:
        lines.append(
            f"(none within {payload['depth']} hop(s) — this means no RELATIONSHIP is recorded, "
            "not that the entity is unused; see Facts below)"
        )

    lines.append("")
    lines.append("Facts (detail tables, NOT graph edges):")
    if services:
        lines.append(_fmt(services))
    else:
        lines.append("(no linux_services rows for this entity)")
    lines.append(payload["facts"]["note"])
    return _truncate_tool_result("\n".join(lines))


@tool
def query_vulnerabilities(
    host: str | None = None, severity: str | None = None, status: str = "open", limit: int = 20
) -> str:
    """
    Query the vulnerability queue (CVEs mapped to hosts) — the same data the
    MCP server's get_vulnerabilities tool exposes, ported to the chat surface.

    Args:
        host: filter by host name — leave blank/None for all
        severity: critical | severe | moderate — leave blank/None for all
        status: open | resolved (default open) — pass '' for all statuses
        limit: max number of rows to return (default 20, max 100)
    """
    limit = min(limit, 100)
    sql = """
        SELECT vq.cve_id, r.name AS host, vq.severity, vq.sla_due, vq.status
        FROM vuln_queue vq
        JOIN resources r ON r.id = vq.resource_id
        WHERE (:host = '' OR r.name = :host)
          AND (:severity = '' OR vq.severity = :severity)
          AND (:status = '' OR vq.status = :status)
        ORDER BY vq.sla_due ASC NULLS LAST
        LIMIT :limit
    """
    rows = _rows(
        sql,
        {"host": host or "", "severity": severity or "", "status": status, "limit": limit},
    )
    if not rows:
        return "No vulnerabilities found matching those filters."
    return _truncate_tool_result(_fmt(rows))


@tool
def query_eol_status(host: str | None = None, days_until_eol: int | None = None) -> str:
    """
    Query the EOL registry — asset end-of-life dates and PCI risk scores, the
    same data the MCP server's get_eol_status tool exposes.

    Args:
        host: filter by resource/asset name — leave blank/None for all
        days_until_eol: only return assets whose eol_date is within this many
            days (or already past), plus assets with an unknown (null) eol_date
            — leave None for all
    """
    # GitLab #186: assets with a NULL eol_date are INCLUDED by the cutoff filter,
    # not excluded. A bare ``eol_date <= cutoff`` drops them (SQL NULL never
    # satisfies a comparison), which hid genuinely overdue products from the
    # urgency query. Unknown means "must be reviewed", not "not applicable".
    sql = """
        SELECT er.asset_name, r.name AS resource_name, er.eol_date, er.pci_risk_score
        FROM eol_registry er
        JOIN resources r ON r.id = er.resource_id
        WHERE (:host = '' OR r.name = :host)
          AND (:cutoff_days IS NULL
               OR er.eol_date IS NULL
               OR er.eol_date <= NOW() + (:cutoff_days || ' days')::interval)
        ORDER BY er.eol_date ASC NULLS LAST
    """
    rows = _rows(
        sql,
        {"host": host or "", "cutoff_days": days_until_eol},
    )
    if not rows:
        return "No EOL registry entries found matching those filters."
    return _truncate_tool_result(_fmt(rows))


@tool
def search_knowledge(query: str, limit: int = 5, format: str = "text") -> str:
    """
    Search the internal knowledge base (runbooks, decision records, architecture
    docs, and other institutional documentation) by meaning and return the most
    relevant passages. Each passage comes back with a numbered source header
    (title, section, similarity score) so you can CITE it as ``[Source N: title]``
    when you use it. Ground documentation answers in these passages rather than
    guessing. An empty result means the knowledge base has nothing on that topic
    (or RAG is disabled) — say so rather than inventing an answer.
    """
    from infra_brain.embeddings import search_knowledge as _search_knowledge

    limit = min(max(int(limit), 1), 20)
    hits = _search_knowledge(query, k=limit)
    if not hits:
        return "No knowledge-base results (RAG may be disabled or nothing indexed on this topic)."
    if format == "json":
        return _truncate_tool_result(json.dumps(hits, default=str))
    # TRK-122 R2: numbered, citable source format. Full chunk text (no [:400]
    # truncation) — 5 hits × ~1300 chars ≈ 6.5K is well under _DEFAULT_MAX_CHARS
    # (12000), and _truncate_tool_result() is the backstop for outliers.
    lines = [
        f"Retrieved {len(hits)} knowledge-base passage(s), ordered by relevance:",
        "",
    ]
    for i, h in enumerate(hits, 1):
        title = h.get("title") or "untitled"
        section = h.get("space") or h.get("source") or "unknown"
        sim = h.get("similarity")
        sim_str = f"similarity {sim}" if sim is not None else "similarity n/a"
        lines.append(f'[{i}] "{title}" — {section} ({sim_str})')
        if h.get("url"):
            lines.append(str(h["url"]))
        lines.append((h.get("text") or "").strip())
        lines.append("")
    lines.append(
        "Cite passages by number or title, e.g. [Source 1: <title>]. If these "
        "passages do not answer the question, say so rather than guessing."
    )
    return _truncate_tool_result("\n".join(lines))


@tool
def propose_host_purpose_update(
    hostname: str,
    purpose: str | None = None,
    vlan: str | None = None,
    subnet: str | None = None,
) -> str:
    """PROPOSE a host purpose / VLAN / subnet change by opening a GitLab merge request for human review.

    This does NOT change any live data or database record. It opens (or refreshes)
    a merge request against the curated ``host_purpose_map.yml`` for a human to
    review; the change takes effect ONLY after a human merges that MR and the next
    repository sync runs. Use this when a user asks to record or correct a host's
    operational purpose, VLAN, or subnet.

    Args:
        hostname: the host to propose a change for. Must already exist in the
            fleet inventory (the ``resources`` table) or the curated host map.
        purpose: proposed operational purpose (leave blank to store blank).
        vlan: proposed VLAN id (leave blank to store blank).
        subnet: proposed subnet in CIDR form, e.g. ``10.0.20.0/24`` (leave blank
            to store blank).

    Returns:
        A concise status string: the opened MR URL and a note that it awaits human
        review, a "not found" message if the host is unknown, or an error string
        if the proposal could not be opened.
    """
    # Step 1 — read-only existence check. Mirrors the resource-lookup convention
    # of the other chat tools (a SELECT via ``_rows``), and also accepts a host
    # already present in the curated host_purpose_map. No mutation here.
    exists = _rows(
        """
        SELECT 1 AS ok FROM resources WHERE name = :name
        UNION
        SELECT 1 AS ok FROM host_purpose_map WHERE LOWER(hostname) = LOWER(:name)
        LIMIT 1
        """,
        {"name": hostname},
    )
    if not exists:
        return f"Host '{hostname}' not found; no MR opened."

    # Step 2 — open the MR through the SINGLE sanctioned write path. proposed=True
    # so the helper NEVER writes the HostPurposeMap DB table; the outbound GitLab
    # write is authorised by the fail-closed write-gate inside create_inventory_mr.
    # A write-gate denial (PermissionError) or any transport failure is surfaced
    # as a clear error string rather than crashing the chat turn or faking success.
    try:
        result = open_host_purpose_map_mr(
            hostname,
            purpose=purpose,
            vlan=vlan,
            subnet=subnet,
            actor="chat-agent",
            proposed=True,
        )
    except PermissionError as exc:
        return f"Proposal for '{hostname}' was blocked by the write gate: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Failed to open a proposal MR for '{hostname}': {exc}"

    # Step 3 — success. No live data changed; the MR awaits a human merge.
    return (
        f"Proposed purpose/VLAN change for '{hostname}': opened MR {result['mr_url']} "
        f"(branch {result['branch']}, {result['action']}). "
        f"This is a proposal only — no live data was changed. It takes effect only "
        f"after a human reviews and merges the MR and the next repo sync runs."
    )
