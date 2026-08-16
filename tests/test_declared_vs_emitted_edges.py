"""CI guard: every RelationshipType must be EITHER registered in RELATIONSHIP_PROPS
with at least one live emit_edge / emit_edges_batch call site in agents/, OR
explicitly listed in exactly one of the four "documented as absent" sets in
db/relationships.py (DEFERRED_RELATIONSHIP_TYPES, MIGRATED_TO_GRAPH_EDGES,
RETIRED_CONTAINMENT_DERIVATIONS, RETIRED_WITH_LEGACY_STORE).

NOTE ON THE (deleted) P5 HANDOVER SET. During the P5 parallel wave this file
temporarily hosted ``P5_WRITER_REMOVED_PENDING_SET_MOVE`` — a holding category
that let the writer-removal branches record "this type's last emitter is gone"
without colliding on db/relationships.py, which the drop branch owned. The
integration folded every member into a canonical set (most into the then-new
RETIRED_WITH_LEGACY_STORE; MEMBER_OF/RUNS_EOL into DEFERRED with the TRK-359
note; BELONGS_TO into MIGRATED) and deleted the set and its two hygiene tests,
exactly as its own contract required.

Failures here mean one of:
  1. A new RelationshipType was added to the enum but no emitter was written yet
     and it was not added to DEFERRED_RELATIONSHIP_TYPES.
  2. A type was wrongly added to DEFERRED_RELATIONSHIP_TYPES when a real emitter
     already exists.
  3. A type exists in RELATIONSHIP_PROPS but has zero call sites — dead code.
  4. A type was claimed MIGRATED_TO_GRAPH_EDGES but no AgentSpec declares it —
     i.e. a deriver was deleted and its replacement never landed (or was later
     removed), which would drop the relationship entirely and silently.
  5. A type was claimed RETIRED_CONTAINMENT_DERIVATIONS (deliberately derived by
     nothing, per §3.1) or RETIRED_WITH_LEGACY_STORE (its writers died with the
     P5 table drop) but something still derives it — the retirement did not
     actually happen, or a deriver came back.
  6. A type is in TWO of the four sets, which cannot both be true and would
     leave the next reader unable to tell which case it is.

Run this with: pytest tests/test_declared_vs_emitted_edges.py -v
"""

import re
from pathlib import Path

import pytest

from infra_brain.db.relationships import (
    DEFERRED_RELATIONSHIP_TYPES,
    MIGRATED_TO_GRAPH_EDGES,
    RELATIONSHIP_PROPS,
    RETIRED_CONTAINMENT_DERIVATIONS,
    RETIRED_WITH_LEGACY_STORE,
    RelationshipType,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_AGENTS_DIR = Path(__file__).parent.parent / "src" / "infra_brain" / "agents"


def _collect_emit_call_sites() -> dict[RelationshipType, list[Path]]:
    """Return a mapping of RelationshipType → list of agent files that reference it.

    Scans every .py in src/infra_brain/agents/ for emit_edge or emit_edges_batch
    calls that reference each RelationshipType member.  We look for patterns:
        RelationshipType.MEMBER_NAME
    inside any emit_edge / emit_edges_batch argument context.  We use a broad scan
    (any occurrence of ``RelationshipType.<name>`` in a file that also calls
    ``emit_edge`` or ``emit_edges_batch``) to keep the regex tractable.
    """
    emit_pattern = re.compile(r"\bemit_edges?(?:_batch)?\s*\(")
    type_pattern = re.compile(r"\bRelationshipType\.([A-Z_]+)\b")

    result: dict[RelationshipType, list[Path]] = {rt: [] for rt in RelationshipType}

    for py_file in _AGENTS_DIR.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        if not emit_pattern.search(source):
            # No emit call in this file — skip scanning for type references
            continue
        for match in type_pattern.finditer(source):
            member_name = match.group(1)
            try:
                rt = RelationshipType[member_name]
            except KeyError:
                continue
            if py_file not in result[rt]:
                result[rt].append(py_file)

    return result


# ------------------------------------------------------------------
# Build the mapping once at module import time (fast — only reads files)
# ------------------------------------------------------------------
_EMIT_SITES: dict[RelationshipType, list[Path]] = _collect_emit_call_sites()


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_every_type_in_relationship_props():
    """Every RelationshipType member must be in RELATIONSHIP_PROPS."""
    missing = [rt for rt in RelationshipType if rt not in RELATIONSHIP_PROPS]
    assert not missing, (
        f"RelationshipType members not registered in RELATIONSHIP_PROPS: "
        f"{[rt.value for rt in missing]}"
    )


@pytest.mark.parametrize("rt", list(RelationshipType))
def test_type_has_emitter_or_is_deferred(rt: RelationshipType):
    """Each RelationshipType must have a live emitter OR be documented as absent.

    "Documented as absent" is FOUR different things and they are kept apart:
    DEFERRED_RELATIONSHIP_TYPES means nothing emits it and the prerequisite work
    is pending (someone should finish it); MIGRATED_TO_GRAPH_EDGES means it IS
    emitted, declaratively, into the other store (do not re-add a deriver);
    RETIRED_CONTAINMENT_DERIVATIONS means it is deliberately derived by nothing,
    anywhere, because it was a fact ABOUT one entity rather than a relationship
    between two (§3.1) — do not re-add a deriver and do not go looking for a
    declaration either; RETIRED_WITH_LEGACY_STORE means it was a genuine
    relationship whose only writers died with the P5 table drop — declarable
    the day someone needs it, as a collector declaration, never a deriver.
    See the comments on those sets in db/relationships.py.

    Intentionally fails if:
    - A type has no emitter AND is in none of the three sets (undocumented gap)
    - A type IS in DEFERRED but ALSO has a live emitter  (stale deferral)
    - A type IS in MIGRATED but ALSO still derives into this store (two writers)
    - A type IS in RETIRED but ALSO still derives into this store (the deletion
      did not happen, or a deriver came back)
    - A type is in more than one of the three sets (mutually exclusive claims)
    """
    is_deferred = rt in DEFERRED_RELATIONSHIP_TYPES
    is_migrated = rt in MIGRATED_TO_GRAPH_EDGES
    is_retired = rt in RETIRED_CONTAINMENT_DERIVATIONS
    is_store_retired = rt in RETIRED_WITH_LEGACY_STORE
    has_emitter = bool(_EMIT_SITES.get(rt))

    claimed = [
        name
        for name, flag in (
            ("DEFERRED_RELATIONSHIP_TYPES", is_deferred),
            ("MIGRATED_TO_GRAPH_EDGES", is_migrated),
            ("RETIRED_CONTAINMENT_DERIVATIONS", is_retired),
            ("RETIRED_WITH_LEGACY_STORE", is_store_retired),
        )
        if flag
    ]
    if len(claimed) > 1:
        pytest.fail(
            f"{rt.value} is in {claimed} — these four sets are mutually "
            f"exclusive; each answers a different question and a type in two of "
            f"them tells the next reader nothing."
        )

    if is_retired and has_emitter:
        pytest.fail(
            f"{rt.value} is in RETIRED_CONTAINMENT_DERIVATIONS — a fact ABOUT an "
            f"entity, deliberately derived by nothing (§3.1 of "
            f"docs/decisions/2026-08-11-graph-first-architecture.md) — but is "
            f"still derived in: {[str(p.name) for p in _EMIT_SITES[rt]]}. Either "
            f"delete that deriver or remove the type from the set."
        )

    if is_deferred and has_emitter:
        pytest.fail(
            f"{rt.value} is in DEFERRED_RELATIONSHIP_TYPES but has live emitters "
            f"in: {[str(p.name) for p in _EMIT_SITES[rt]]}. "
            f"Remove it from DEFERRED_RELATIONSHIP_TYPES."
        )

    if is_store_retired and has_emitter:
        pytest.fail(
            f"{rt.value} is in RETIRED_WITH_LEGACY_STORE — a genuine relationship "
            f"whose only writers died with the P5 drop of resource_relationships — "
            f"but something still derives it in: "
            f"{[str(p.name) for p in _EMIT_SITES[rt]]}. If the type is being "
            f"revived, it must come back as an AgentSpec.emits_edges declaration "
            f"into graph_edges (and move to MIGRATED_TO_GRAPH_EDGES), never as a "
            f"deriver — the store derivers wrote to no longer exists."
        )

    if is_migrated and has_emitter:
        pytest.fail(
            f"{rt.value} is in MIGRATED_TO_GRAPH_EDGES — declared on an AgentSpec "
            f"and written to graph_edges — but is ALSO still derived into "
            f"resource_relationships by {[str(p.name) for p in _EMIT_SITES[rt]]}. "
            f"Two writers for one relationship is a slow-motion contradiction: "
            f"both stores upsert, so it looks fine until they disagree."
        )

    if not claimed and not has_emitter:
        pytest.fail(
            f"{rt.value} has no live emit_edge/emit_edges_batch call site in "
            f"src/infra_brain/agents/*.py and is in none of "
            f"DEFERRED_RELATIONSHIP_TYPES / MIGRATED_TO_GRAPH_EDGES / "
            f"RETIRED_CONTAINMENT_DERIVATIONS / RETIRED_WITH_LEGACY_STORE. "
            f"Either add an emitter, or add it to one of those sets with a "
            f"comment explaining which case it is."
        )


def test_deferred_types_have_no_emitters():
    """Convenience aggregate: all deferred types must have zero live emitters."""
    wrongly_deferred = [rt for rt in DEFERRED_RELATIONSHIP_TYPES if _EMIT_SITES.get(rt)]
    assert not wrongly_deferred, (
        f"These types are in DEFERRED_RELATIONSHIP_TYPES but have live emitters: "
        f"{[(rt.value, [p.name for p in _EMIT_SITES[rt]]) for rt in wrongly_deferred]}"
    )


def test_migrated_types_are_actually_declared_somewhere():
    """The other half of a deletion: the replacement has to exist.

    Deleting a deriver is only safe while something else writes the
    relationship. If a later change removes the declaration too, this is where
    that shows up — otherwise the type would simply stop being produced, and
    every test above would still pass because "no emitter" is exactly what
    MIGRATED_TO_GRAPH_EDGES asserts.
    """
    from infra_brain.etl.spec import agent_specs

    declared: dict[str, list[str]] = {}
    for domain, spec in agent_specs().items():
        for edge in spec.emits_edges:
            declared.setdefault(edge.type, []).append(domain)

    missing = [rt.value for rt in MIGRATED_TO_GRAPH_EDGES if rt.value not in declared]
    assert not missing, (
        f"These types were removed from the resource_relationships derivers on the "
        f"grounds that an AgentSpec declares them, but no spec does: {missing}. "
        f"Either restore the declaration or the deriver — the relationship is "
        f"currently produced by nothing."
    )


# The P5 handover-set hygiene tests were deleted with the handover set itself
# at integration — the success condition its own contract named. The mutual-
# exclusion and no-emitter claims they enforced are carried for all four
# canonical sets by ``test_type_has_emitter_or_is_deferred`` above.
