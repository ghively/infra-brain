"""AgentSpec — the single declarative registry entry for a domain agent.

Phase 1 Task 5 (TRK-047): agent metadata used to live in FOUR places —
class attributes on ``ETLConnector`` subclasses (domain/schedule/skip_hook/
dispatchable), ``coverage.DEFAULT_SCHEDULES``, ``fleet_health._DOMAIN_MAX_AGE``
and ``callbacks/freshness.DOMAIN_EXPECTED_MAX_AGE`` — the latter two already
disagreeing (octopus 24h vs 26h). ``AgentSpec`` collapses all of it into one
frozen dataclass declared on each agent class as ``spec: ClassVar[AgentSpec]``.

``ETLConnector.__init_subclass__`` derives the legacy class attributes
(``domain``/``schedule``/``skip_hook``/``dispatchable``) from ``spec`` so
``supervisor.py`` and ``scheduler.py`` keep working UNMODIFIED. Consumers of
the old shadow tables derive from specs via the helpers below.

See docs/ARCHITECTURE.md ("The AgentSpec contract") for the tier semantics
and the "adding agent #29" checklist.
"""

import enum
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal


class Tier(enum.Enum):
    """Target-architecture tier (orchestration v2.1) for a domain agent.

    COLLECTOR   — deterministic external-system collectors (linux, vsphere, ...)
    RECONCILER  — cross-domain identity/graph reconciliation (host_reconcile, ...)
    REASONER    — analysis over already-collected data (drift, compliance, ...)
    REPORTER    — read-only reporting outside the sweep graph (fleet_health, ...)
    ON_DEMAND   — dispatched explicitly, never part of the sweep graph
    """

    COLLECTOR = "collector"
    RECONCILER = "reconciler"
    REASONER = "reasoner"
    REPORTER = "reporter"
    ON_DEMAND = "on_demand"


# ---------------------------------------------------------------------------
# Graph contribution (P0 of docs/decisions/2026-08-11-graph-first-architecture.md)
# ---------------------------------------------------------------------------
#
# Until now every AgentSpec field governed WHEN a collector runs; none said
# what it contributes to the knowledge graph. That knowledge lived in
# agents/graph_maintenance.py -- 6,367 lines importing domain tables directly
# -- so adding a collector was one scaffolded command but getting its data
# into the graph meant editing a monolith. The three fields below move graph
# contribution into the plugin contract. All are optional and default empty,
# so every shipped spec is untouched and P0 is a strict no-op.

#: Fields of a ``resources`` row a NodeSpec may reference. ``metadata.<key>``
#: reaches into the JSONB blob. Closed on purpose: a typo'd reference must
#: fail at declaration time, not silently materialise nothing.
_RESOURCE_FIELDS: frozenset[str] = frozenset({"name", "type", "domain", "source", "zone"})

#: Fields of a ``graph_nodes`` row an EdgeSpec may reference on either side.
#: ``attributes.<key>`` reaches into the JSONB blob a NodeSpec populated.
_NODE_FIELDS: frozenset[str] = frozenset({"name", "natural_key", "node_type", "source"})

#: Prefix a ``NodeSpec`` uses to reference a value taken from a ``ChildSpec``'s
#: rows rather than from the ``resources`` row itself.
_ROWS_PREFIX = "rows"


class EdgeDirection(enum.Enum):
    """Which way an ``EdgeSpec``'s edge is WRITTEN, relative to its join.

    ``from_node``/``to_node`` always describe the **join**, never the arrow:
    ``from_node`` is the side that carries the key pointing at the other, so
    the join is always functional (many join rows resolve to one ``to_node``)
    and always starts at an entity the declaring collector owns. ``direction``
    then says which way the resulting edge is stored.

    FORWARD  — write ``from_node → to_node``. A many-to-one edge; N sources
               each get one edge to their single target. This is what every
               declaration written before this field existed means, so it is
               the default and nothing changes for them.
    INVERSE  — write ``to_node → from_node``. The SAME join read backwards:
               the N sources still each resolve to one target, and the N edges
               are stored fanning OUT of it. This is how a one-to-many
               relationship is declared.

    WHY THIS AND NOT A ``cardinality`` FIELD THAT RELAXES THE MATCH.
    ``graph_engine._target_index`` maps a normalised key to ONE target node and
    drops any key two targets claim, because from the engine's side N nodes
    answering to one key is indistinguishable from a contradiction. That rule
    is correct and INVERSE does not touch it: the join it runs is byte-for-byte
    the many-to-one join, still refusing to guess between two claimants. Only
    the stored arrow is reversed. A one-to-many relationship's key lives on the
    "many" side by construction — one project defines many files, and it is the
    FILE that carries ``project_id`` — so "one-to-many" and "many-to-one read
    backwards" are the same fact, and declaring the direction is enough. The
    alternative (index a key to a LIST of targets) would have had to decide
    when N claimants is a fan-out and when it is a contradiction, which is the
    one thing the engine cannot know.

    WHAT INVERSE DOES NOT BUY. A many-to-many relationship — neither side
    functional — is still not expressible, and still fails the honest way: the
    contested side is reported ambiguous and no edge is written. See
    ``tests/test_graph_engine.py::test_a_many_to_many_relationship_is_still_not_expressible``.
    """

    FORWARD = "forward"
    INVERSE = "inverse"


def _validate_field_ref(ref: str, allowed: frozenset[str], blob: str, where: str) -> None:
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"{where}: field reference must be a non-empty string, got {ref!r}")
    if ref.startswith(f"{blob}."):
        if len(ref) <= len(blob) + 1:
            raise ValueError(f"{where}: field reference {ref!r} names no {blob} key")
        return
    if ref not in allowed:
        raise ValueError(
            f"{where}: unknown field reference {ref!r} — expected one of "
            f"{sorted(allowed)} or '{blob}.<key>'"
        )


@dataclass(frozen=True)
class ChildSpec:
    """Rows of a collector-owned CHILD TABLE, reached from a ``resources`` row.

    THE GAP THIS CLOSES. A ``NodeSpec``'s world was a ``resources`` row plus its
    ``metadata`` blob, and nothing else. Several real facts do not live there:
    a compose file's images are rows of ``compose_services``, an inventory's
    managed hosts are rows of ``ansible_inventory_hosts``. Those tables have no
    ``resources`` row of their own and no representation in any resource's
    ``metadata``, so the relationships that join on them could not be declared
    at all — they stayed hand-written in ``graph_maintenance``. A ``ChildSpec``
    gives a declaration reach into exactly those rows, and no further.

    HOW THE ENGINE STAYS DOMAIN-FREE. ``path`` is a chain of ``(table name, FK
    column)`` hops spelled as PLAIN STRINGS. ``graph_engine`` resolves them
    against the shared SQLAlchemy ``MetaData`` that every model registers into —
    it looks the table up by name and reads its primary key — so it still
    imports no domain model and still branches on nothing. A wrong table or
    column name is an error the engine reports for that one declaration; it is
    not something the engine can be taught to special-case.

    HOW THE PATH IS WALKED. Hop *i* joins ``table.c[fk_column]`` to the PRIMARY
    KEY of the previous table, the first hop's previous table being
    ``resources``. That is a strict parent-to-child descent by single-column
    primary key — deliberately the narrowest useful grammar:

        ``resources.id  <-  iac_files.resource_id``
        ``iac_files.id  <-  compose_services.iac_file_id``

    WHAT IT IS NOT. It is not a general join language. It cannot join on a
    business key (``gitlab_projects.gitlab_project_id`` to
    ``iac_files.gitlab_project_id``), cannot descend into another collector's
    tables from your own resource, and cannot filter. Each of those would turn
    the declaration into a query builder and hand the engine, or the
    declaration, exactly the domain knowledge this contract exists to remove.
    A relationship that needs one of them is still not declarable. What it can
    reach is often enough to declare the relationship from a DIFFERENT anchor —
    that is how ANSIBLE_MANAGES was finally migrated (TRK-354 Option A: the
    inventory FILE's own descent, rather than a business-key hop from the
    project) — but that re-anchoring is a product decision, never something the
    grammar should be widened to avoid.

    key    -- the name the gathered value(s) appear under in
              ``graph_nodes.attributes``, and the name a ``rows.<key>`` field
              reference resolves. Chosen by the declaration, not derived from
              the column, so a rename in the DB does not silently rename a
              graph attribute an EdgeSpec joins on.
    path   -- ordered ``(table_name, fk_column)`` hops, at least one.
    column -- the column on the LAST table whose value is taken. Scalar
              columns only: a JSON-list column (``ansible_playbook_plays.hosts``)
              is REFUSED at materialisation time rather than flattened, the
              same refusal ``graph_engine._resolve`` already gives a
              list-valued field reference. Flattening is a further contract
              decision, not an implementation detail.
    """

    key: str
    path: tuple[tuple[str, str], ...]
    column: str

    def __post_init__(self) -> None:
        where = f"ChildSpec({self.key!r})"
        for name in ("key", "column"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{where}: {name} must be a non-empty string, got {value!r}")
        if not isinstance(self.path, tuple) or not self.path:
            raise ValueError(
                f"{where}: path must be a non-empty tuple of (table_name, fk_column) hops — "
                "AgentSpec is frozen/hashable, so a list is neither safe nor allowed"
            )
        for hop in self.path:
            if not isinstance(hop, tuple) or len(hop) != 2:
                raise ValueError(
                    f"{where}: path hop {hop!r} must be a (table_name, fk_column) pair"
                )
            table_name, fk_column = hop
            if not isinstance(table_name, str) or not table_name:
                raise ValueError(f"{where}: path hop {hop!r} has an empty table name")
            if not isinstance(fk_column, str) or not fk_column:
                raise ValueError(f"{where}: path hop {hop!r} has an empty FK column")


@dataclass(frozen=True)
class RowGather:
    """A fan-out list carried by a ``from_rows`` (junction) node, grouped by
    the junction VALUE rather than by parent resource.

    THE GAP THIS CLOSES (TRK-359). A ``from_rows`` node may not ``gathers``:
    its identity recurs under many parents, so a per-PARENT gathered list
    would mean "whichever parent was materialised last" — that refusal stands.
    But the two P5 accepted losses (``MEMBER_OF``, ``RUNS_EOL``) each needed a
    junction node to carry the member list an edge fans out over: an inventory
    GROUP's hosts, an EOL PRODUCT's hosts. The fix is a different GROUPING,
    not a softer rule: a ``RowGather`` is keyed by the junction value itself,
    so a value that recurs under many parents accumulates the deterministic
    UNION of what all of them say — order-free, last-writer-free.

    Two shapes, one per accepted loss:

    * ``path`` NON-EMPTY — deeper descent (the ``MEMBER_OF`` shape). Hops
      continue BELOW the junction table by the same strict single-column-PK
      parent-to-child walk ``ChildSpec`` uses, relative to the junction table
      (``from_rows``'s last hop): ``ansible_inventory_groups`` →
      ``(ansible_inventory_hosts, group_id)``. ``column`` is read off the
      last table; a JSON-list value is REFUSED, never flattened.
    * ``path`` EMPTY — anchor gather (the ``RUNS_EOL`` shape). The gathered
      value is a field of the ANCHORING ``resources`` row the junction row
      descends from (``eol_registry.resource_id`` IS the host that runs the
      product). ``column`` is then a resources FIELD REF (``name``,
      ``metadata.<key>``, …) and a typo fails HERE, at declaration time.

    key    -- the attribute name on the junction node, and the name an
              ``EdgeSpec.from_key`` (with ``from_key_multi=True``) joins on.
    path   -- extra hops below the junction table; ``()`` for anchor gather.
    column -- column on the last hop's table, or a resources field ref when
              ``path`` is empty.
    """

    key: str
    path: tuple[tuple[str, str], ...]
    column: str

    def __post_init__(self) -> None:
        where = f"RowGather({self.key!r})"
        for name in ("key", "column"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{where}: {name} must be a non-empty string, got {value!r}")
        if not isinstance(self.path, tuple):
            raise ValueError(
                f"{where}: path must be a tuple of (table_name, fk_column) hops — "
                "empty for an anchor gather; AgentSpec is frozen/hashable, so a "
                "list is neither safe nor allowed"
            )
        for hop in self.path:
            if not isinstance(hop, tuple) or len(hop) != 2:
                raise ValueError(
                    f"{where}: path hop {hop!r} must be a (table_name, fk_column) pair"
                )
            table_name, fk_column = hop
            if not isinstance(table_name, str) or not table_name:
                raise ValueError(f"{where}: path hop {hop!r} has an empty table name")
            if not isinstance(fk_column, str) or not fk_column:
                raise ValueError(f"{where}: path hop {hop!r} has an empty FK column")
        if not self.path:
            # Anchor gather: the "column" is a resources field ref, and the
            # closed-vocabulary check runs NOW — a typo that only failed at
            # materialisation time would silently gather nothing per row.
            _validate_field_ref(self.column, _RESOURCE_FIELDS, "metadata", where)


@dataclass(frozen=True)
class NodeSpec:
    """One kind of entity a collector contributes to the graph.

    Nodes are read from the generic ``resources`` table, NOT from a per-source
    ORM class. That is a deliberate divergence from the design doc's sketch
    (``source_table=R7Asset``) and the reason the engine can be genuinely
    domain-free: ``resources`` is the one table every collector already writes,
    with its payload in ``metadata``, so the engine needs no import of, and no
    compile-time knowledge about, any domain model.

    ``gathers`` / ``from_rows`` widen that world by exactly one step, to the
    CHILD ROWS of a resource — see ``ChildSpec``. The engine reaches them
    through the shared SQLAlchemy ``MetaData`` by table name, so it still
    imports no domain model. Everything a declaration can see is still either a
    ``resources`` row, its ``metadata``, or rows that descend from it by
    primary key.

    type            -- ``graph_nodes.node_type``. A free string, per-source by
                       convention ("LinuxHost", "HomelabService") — never a
                       unified "Host", per the Phase 2 rule. Deliberately NOT
                       constrained to ``GraphNodeType``: that enum is a closed,
                       test-locked vocabulary for the hand-written Phase 2
                       emitters, and routing declarations through it would
                       reintroduce exactly the central edit this contract
                       exists to remove.
    resource_type   -- ``resources.type`` filter ("linux_host").
    domain          -- ``resources.domain`` filter. Defaults to the declaring
                       AgentSpec's own domain; a collector normally only
                       contributes its own rows.
    natural_key     -- field ref giving the node's stable per-source identity.
                       A row whose key resolves empty is SKIPPED, never
                       materialised under a blank key.
    name            -- field ref giving the display name. Defaults to "name".
    attributes      -- ``metadata`` keys copied onto ``graph_nodes.attributes``.
                       An edge's join key must be listed here: the engine
                       matches graph-side, so a value a NodeSpec did not carry
                       across is not available to an EdgeSpec.
    resource_backed -- False for shared value nodes (a CVE, a tag) that have no
                       single owning resource row.
    is_host_identity -- this node type denotes a HOST-SHAPED MACHINE, i.e. a
                       thing another source may independently know under its own
                       name, so it is a candidate endpoint for a cross-source
                       ``SAME_AS``. Default False: a value node (a CVE, an
                       image), a document, a project or a service is not a
                       machine, and offering it to the identity resolver would
                       invite merging two unrelated things that happen to be
                       spelled alike.

                       WHY THIS FLAG EXISTS AT ALL (P5). The resolver's
                       candidate population is ``graph_phase3.HOST_NODE_TYPES``,
                       which was a hardcoded tuple written before declarative
                       ``NodeSpec``s existed. ``agents/linux.py`` then declared
                       ``LinuxHost``, the live estate materialised it, and the
                       resolver still never loaded it — not "uncertain, so
                       queued" but INVISIBLE: no node, no candidate, no review
                       question. Nothing failed, because nothing connected the
                       declaration to the tuple. This flag is that connection:
                       ``tests/test_p5_issameas_resolver_coverage.py`` asserts
                       every ``is_host_identity`` NodeSpec appears in
                       ``HOST_NODE_TYPES``, so declaring a host node without
                       widening the resolver is now a test failure rather than a
                       silent coverage hole.

                       It is a DECLARATION, not a derivation: ``HOST_NODE_TYPES``
                       stays an explicit tuple. Deriving it at resolve time would
                       mean calling ``agent_specs()``, which imports every
                       collector module — an objection first recorded at the
                       late ``GRAPH_SERVED_EDGE_TYPES`` (removed with the
                       legacy walk in P5) — and
                       would make the resolver's population depend on import
                       order. Explicit set, mechanically guarded, is the shape
                       this codebase already chose for the identical problem.
    gathers         -- child tables whose values ride onto THIS node's
                       ``attributes``, one list per ``ChildSpec.key``, distinct
                       and sorted. This is how a fact that lives in a child
                       table becomes available to an ``EdgeSpec``, which matches
                       graph-side and can therefore only see what a node
                       carries. A gathered attribute is a LIST, so an edge that
                       joins on one must say ``from_key_multi=True``.
    from_rows       -- when set, this node's IDENTITY comes from a child row
                       rather than from the ``resources`` row: one node per
                       DISTINCT value of ``ChildSpec.column`` across every
                       matching resource. ``resource_type``/``domain`` still
                       select which resources' children are read; the resource
                       itself becomes the row's provenance, not its identity.

                       ``natural_key`` and ``name`` must then both be
                       ``rows.<key>`` — a value that recurs under many parents
                       (one image in six compose files) has no single owning
                       resource, so ``resource_backed`` must be False and the
                       node carries ``{key: value}`` plus whatever
                       ``row_gathers`` adds. Per-parent child entities are
                       deliberately NOT expressible this way: they would need a
                       composite identity, which is a further contract decision.
    row_gathers     -- fan-out lists carried by a ``from_rows`` node, grouped by
                       the junction VALUE (see ``RowGather``). This is the
                       junction grammar (TRK-359): it is what lets an edge be
                       declared where one endpoint is a node materialised from
                       child rows — the group node carries its member hosts,
                       the product node carries the hosts that run it — with
                       the edge itself spelled in the EXISTING vocabulary
                       (``from_key_multi`` + ``EdgeDirection.INVERSE``).
                       Requires ``from_rows``; the per-parent ``gathers``
                       refusal on such nodes is unchanged.
    """

    type: str
    resource_type: str
    natural_key: str = "name"
    name: str = "name"
    attributes: tuple[str, ...] = ()
    resource_backed: bool = True
    is_host_identity: bool = False
    domain: str | None = None
    gathers: tuple[ChildSpec, ...] = ()
    from_rows: ChildSpec | None = None
    row_gathers: tuple[RowGather, ...] = ()

    def __post_init__(self) -> None:
        where = f"NodeSpec({self.type!r})"
        if not self.type or not self.resource_type:
            raise ValueError(f"{where}: type and resource_type are required")
        if len(self.type) > 64:
            raise ValueError(f"{where}: node type must fit graph_nodes.node_type (64 chars)")
        if self.from_rows is not None and not isinstance(self.from_rows, ChildSpec):
            raise ValueError(f"{where}: from_rows must be a ChildSpec, got {self.from_rows!r}")
        for ref_name in ("natural_key", "name"):
            self._validate_identity_ref(ref_name, getattr(self, ref_name), where)
        if not isinstance(self.attributes, tuple):
            raise ValueError(f"{where}: attributes must be a tuple (AgentSpec is frozen/hashable)")
        if not isinstance(self.gathers, tuple):
            raise ValueError(f"{where}: gathers must be a tuple (AgentSpec is frozen/hashable)")
        keys = [child.key for child in self.gathers]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{where}: duplicate gathers key in {sorted(keys)}")
        clash = set(keys) & set(self.attributes)
        if clash:
            raise ValueError(
                f"{where}: gathers key(s) {sorted(clash)} also named in attributes — one "
                "attribute name cannot mean both a metadata key and a gathered child column"
            )
        if self.is_host_identity and not self.resource_backed:
            raise ValueError(
                f"{where}: is_host_identity requires resource_backed — the identity resolver "
                "gates pairs in resources.id space (graph_phase3.resource_pair_gate) and reads "
                "host_reconcile's ambiguity legs by resource_id, so a node with no owning "
                "resources row cannot be scored, gated, or reviewed as a machine"
            )
        if self.from_rows is not None:
            if self.resource_backed:
                raise ValueError(
                    f"{where}: a from_rows node is identified by a child VALUE, which recurs "
                    "under many resources — it has no single owning row, so resource_backed "
                    "must be False"
                )
            if self.gathers:
                raise ValueError(
                    f"{where}: a from_rows node cannot also gather — its identity is shared "
                    "across parents, so a per-parent gathered list would mean whichever "
                    "parent was materialised last. A fan-out grouped by the junction VALUE "
                    "is what row_gathers is for"
                )
            if self.attributes:
                raise ValueError(
                    f"{where}: a from_rows node cannot copy resources.metadata — the metadata "
                    "belongs to one parent, the node belongs to all of them"
                )
        if not isinstance(self.row_gathers, tuple):
            raise ValueError(f"{where}: row_gathers must be a tuple (AgentSpec is frozen/hashable)")
        if self.row_gathers:
            if self.from_rows is None:
                raise ValueError(
                    f"{where}: row_gathers requires from_rows — a per-value gather is "
                    "grouped by the junction value, so there must be one; a resource-backed "
                    "node gathers per resource with `gathers` instead"
                )
            for gather in self.row_gathers:
                if not isinstance(gather, RowGather):
                    raise ValueError(
                        f"{where}: row_gathers entries must be RowGather, got {gather!r}"
                    )
                if gather.key == self.from_rows.key:
                    raise ValueError(
                        f"{where}: row_gathers key {gather.key!r} shadows the from_rows key — "
                        "one attribute name cannot mean both the junction value and a "
                        "gathered list"
                    )
            row_keys = [g.key for g in self.row_gathers]
            if len(set(row_keys)) != len(row_keys):
                raise ValueError(f"{where}: duplicate row_gathers key in {sorted(row_keys)}")

    def _validate_identity_ref(self, ref_name: str, ref: str, where: str) -> None:
        """``natural_key``/``name``: a resources field ref, or a ``rows.<key>`` one."""
        if isinstance(ref, str) and ref.startswith(f"{_ROWS_PREFIX}."):
            if self.from_rows is None:
                raise ValueError(
                    f"{where}: {ref_name}={ref!r} references child rows, but the spec declares "
                    "no from_rows"
                )
            if ref != f"{_ROWS_PREFIX}.{self.from_rows.key}":
                raise ValueError(
                    f"{where}: {ref_name}={ref!r} does not name the declared from_rows key "
                    f"{self.from_rows.key!r}"
                )
            return
        if self.from_rows is not None:
            raise ValueError(
                f"{where}: {ref_name}={ref!r} reads the resources row, but this node is "
                f"identified by child rows — expected '{_ROWS_PREFIX}.{self.from_rows.key}'"
            )
        _validate_field_ref(ref, _RESOURCE_FIELDS, "metadata", where)


@dataclass(frozen=True)
class EdgeSpec:
    """One relationship a collector contributes, declared not derived.

    The join is expressed graph-side: take ``from_key`` off each node of type
    ``from_node``, take ``to_key`` off each node of type ``to_node``, run both
    through the named ``key_normalizer``, and connect equal keys. The engine
    therefore needs no knowledge of either source's schema.

    THE JOIN IS ALWAYS FUNCTIONAL AND ALWAYS STARTS AT ``from_node``. Each
    ``from_node`` resolves to at most ONE ``to_node``; a key two targets claim
    is a contradiction and is refused, never guessed. ``direction`` decides
    which way the resulting edge is STORED, which is what makes a one-to-many
    relationship declarable without weakening that rule — see ``EdgeDirection``.

    ``to_node`` may be declared by a DIFFERENT collector — that is the whole
    point of a cross-source graph. Nodes are materialised for every spec before
    any edge is, so declaration order does not matter. If no node of the target
    type has a matching key, NO edge is written; the engine never invents a
    target.

    type          -- ``graph_edges.edge_type``, a free string for the same
                     reason ``NodeSpec.type`` is (``GraphEdgeType`` is a
                     test-locked closed vocabulary for the Phase 2 emitters).
    from_node     -- a node type THIS spec declares in ``emits_nodes``, and the
                     side the join starts at. The ownership rule is about which
                     rows a collector may make assertions FROM (its own), not
                     about which end of the stored arrow they land on: under
                     ``direction=INVERSE`` the collector still enumerates only
                     its own nodes and still asserts one fact per node, it
                     merely records that fact pointing the other way.
    to_node       -- any node type, from any collector.
    from_key/to_key -- field refs on the respective ``graph_nodes`` rows.
    direction     -- ``EdgeDirection.FORWARD`` (default; store from_node →
                     to_node, a many-to-one edge) or ``INVERSE`` (store
                     to_node → from_node, the same join fanning OUT).
    from_key_multi -- the SOURCE key holds SEVERAL values (a
                     ``NodeSpec.gathers`` list: the images one compose file
                     runs). Each value is resolved through the SAME
                     ``_target_index`` — one target or a refusal — and each hit
                     writes one edge, so a source node makes N assertions
                     instead of 1.

                     THIS IS A DIFFERENT AXIS FROM ``direction`` AND FROM THE
                     AMBIGUITY RULE. ``direction`` reverses the stored arrow of
                     one assertion. This says how many assertions a node makes.
                     Neither touches ``_target_index``: a key claimed by two
                     targets is still a contradiction and still refused, which
                     is why
                     ``tests/test_graph_engine.py::test_a_many_to_many_relationship_is_still_not_expressible``
                     passes unchanged — its refusal is about two nodes sharing
                     an identity, not about how many keys the source held.
                     What this DOES make declarable is a many-to-many whose
                     other side is a value node with a unique key (a container
                     image), because every one of the several keys still
                     resolves to exactly one target.

                     Opt-in on purpose. Silently fanning out whenever a value
                     happened to be a list would change what every existing
                     declaration means: today a list-valued key resolves to
                     nothing (``_resolve`` refuses it) and writes no edge.
    key_normalizer -- name from ``etl.keys.KEY_NORMALIZERS``.
    method/confidence -- provenance honesty (see db/models/graph.py). A
                     name-matched edge is ``deterministic_match`` and may NOT
                     claim 1.000; only a real FK-strength ``declared`` join may.
                     Enforced here AND at the store, because a spec is read
                     long before any session exists.
    """

    type: str
    from_node: str
    to_node: str
    from_key: str
    to_key: str
    method: str = "deterministic_match"
    confidence: Decimal = Decimal("0.990")
    key_normalizer: str = "exact"
    direction: EdgeDirection = EdgeDirection.FORWARD
    from_key_multi: bool = False

    @property
    def is_inverse(self) -> bool:
        """True when the stored edge runs ``to_node → from_node``."""
        return self.direction is EdgeDirection.INVERSE

    def written_as(self) -> tuple[str, str]:
        """``(edge source node type, edge target node type)`` as STORED.

        The join sides and the arrow sides differ under ``INVERSE``; anything
        describing the graph to a reader (introspection, tests, docs) wants
        this pair, not ``(from_node, to_node)``.
        """
        return (self.to_node, self.from_node) if self.is_inverse else (self.from_node, self.to_node)

    def __post_init__(self) -> None:
        from infra_brain.etl.keys import KEY_NORMALIZER_NAMES  # noqa: PLC0415

        where = f"EdgeSpec({self.type!r})"
        if not self.type or not self.from_node or not self.to_node:
            raise ValueError(f"{where}: type, from_node and to_node are required")
        if len(self.type) > 64:
            raise ValueError(f"{where}: edge type must fit graph_edges.edge_type (64 chars)")
        _validate_field_ref(self.from_key, _NODE_FIELDS, "attributes", where)
        _validate_field_ref(self.to_key, _NODE_FIELDS, "attributes", where)
        # Accept the enum or its value, mirroring ``method``'s coercion below,
        # so a declaration never has to import the enum just to say "inverse".
        # A typo must fail HERE, at declaration time: a silently-unrecognised
        # direction would store every edge backwards.
        try:
            object.__setattr__(self, "direction", EdgeDirection(self.direction))
        except ValueError:
            raise ValueError(
                f"{where}: unknown direction {self.direction!r} — expected one of "
                f"{sorted(d.value for d in EdgeDirection)}"
            ) from None
        if not isinstance(self.from_key_multi, bool):
            raise ValueError(f"{where}: from_key_multi must be a bool, got {self.from_key_multi!r}")
        if self.key_normalizer not in KEY_NORMALIZER_NAMES:
            raise ValueError(
                f"{where}: unknown key_normalizer {self.key_normalizer!r} — "
                f"expected one of {sorted(KEY_NORMALIZER_NAMES)}"
            )
        method = getattr(self.method, "value", self.method)
        object.__setattr__(self, "method", method)
        conf = Decimal(self.confidence)
        object.__setattr__(self, "confidence", conf)
        if conf < Decimal("0") or conf > Decimal("1.000"):
            raise ValueError(f"{where}: confidence must be within [0, 1.000], got {conf}")
        if conf >= Decimal("1.000") and method != "declared":
            raise ValueError(
                f"{where}: confidence 1.000 is reserved for method='declared' — a "
                f"{method!r} join resolves a mutable display name and can mis-resolve"
            )


@dataclass(frozen=True)
class AgentSpec:
    """Declarative metadata for one domain agent — the single source of truth.

    schedule       — 5-field cron string, or None (hook-/graph-driven, or
                     on-demand only).
    max_staleness  — freshness window (cadence + slack) before the domain is
                     considered stale; absorbs the former
                     ``fleet_health._DOMAIN_MAX_AGE`` and
                     ``freshness.DOMAIN_EXPECTED_MAX_AGE`` tables. None means
                     the domain is not freshness-monitored (hook-driven
                     agents like drift/notification).
    skip_hook      — this domain must NOT re-trigger supervisor.py's
                     _post_collection_hook() after dispatch.
    dispatchable   — False excludes the class from AGENT_REGISTRY entirely.
    retired        — this collector is switched off by a standing decision: its
                     upstream system does not exist in this environment and is
                     not going to. A retired domain is NOT scheduled, NOT a
                     sweep member, NOT freshness-monitored, and NOT
                     dispatchable — but it stays in AGENT_REGISTRY, keeps every
                     other field of its spec (including the cron string it
                     WOULD run on), and stays importable and testable. It is an
                     OFF switch, not a deletion.

                     Distinct from the three neighbouring levers, which it
                     deliberately does not replace:
                       * ``dispatchable=False`` — removes the class from the
                         registry entirely, so the operator sees nothing at
                         all. Retired keeps the row visible, marked off.
                       * ``dispatchable__<domain>`` (runtime_flags.py) — a
                         LIVE, temporary operator pause on a collector that is
                         otherwise expected to run; it records a ``skipped``
                         collection_runs row each cycle precisely so the pause
                         stays visible. Retired writes nothing, because a
                         permanently-off collector reporting "skipped" daily is
                         the noise this field exists to remove.
                       * ``CollectorSkipped`` on missing credentials — "not
                         configured yet", which reads as a gap to be closed.
                         Retired says "not configured, on purpose, ever".

                     Re-enabling needs no code change: see ``retired_domains()``
                     for the ``collection_revived_domains`` override.
    collect_timeout_seconds
                   — per-domain override for the collect() wall-clock guard
                     (BaseAgent._call_with_timeout). None means "use the global
                     ``settings.collect_timeout_seconds``". A domain whose full
                     pass legitimately runs longer than the global default
                     (graph_maintenance — TRK-117) sets this. IMPORTANT: this
                     override is coupled to the Redis dedup lock TTL — see
                     ``dedup.default_ttl_seconds`` /
                     ``collect_timeout_for_domain`` below; the lock must always
                     outlive the (possibly extended) collect phase or a
                     duplicate dispatch could overlap the running one.

    emits_nodes    — entities this collector contributes to the knowledge
                     graph. Empty (the default) means it contributes none,
                     which is true of every spec that predates this field.
    emits_edges    — relationships it contributes. Every ``from_node`` must be
                     declared in ``emits_nodes``; ``to_node`` may belong to any
                     collector. ``from_node`` is the side the JOIN starts at,
                     which under ``EdgeDirection.INVERSE`` is not the side the
                     stored arrow starts at — the ownership rule constrains the
                     former (whose rows may I assert about) and deliberately
                     not the latter.
    identity_keys  — how THIS source names real-world things, for cross-source
                     identity resolution ("hostname", "mac_address", "serial").
                     Declared now so a source states it once, in the same place
                     as everything else about it, instead of it being implicit
                     in ``host_reconcile``'s hardcoded per-source legs.
                     NOT yet consumed — the resolver switch is P2 work and
                     deliberately out of P0/P1 scope; this field is the
                     declaration half only.
    """

    domain: str
    tier: Tier
    schedule: str | None
    max_staleness: timedelta | None
    skip_hook: bool = False
    dispatchable: bool = True
    retired: bool = False
    collect_timeout_seconds: int | None = None
    emits_nodes: tuple[NodeSpec, ...] = field(default_factory=tuple)
    emits_edges: tuple[EdgeSpec, ...] = field(default_factory=tuple)
    identity_keys: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("emits_nodes", "emits_edges", "identity_keys"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(
                    f"AgentSpec({self.domain!r}): {name} must be a tuple — AgentSpec is "
                    "frozen and is used as a dict value/compared; a list is neither "
                    "hashable nor safe to share between instances"
                )
        declared = {n.type for n in self.emits_nodes}
        if len(declared) != len(self.emits_nodes):
            raise ValueError(f"AgentSpec({self.domain!r}): duplicate node type in emits_nodes")
        for edge in self.emits_edges:
            if edge.from_node not in declared:
                raise ValueError(
                    f"AgentSpec({self.domain!r}): edge {edge.type!r} starts at "
                    f"{edge.from_node!r}, which is not in emits_nodes {sorted(declared)} — "
                    "a collector may only emit edges out of entities it owns"
                )


def agent_specs() -> dict[str, AgentSpec]:
    """Domain -> AgentSpec for every registered agent.

    Imports supervisor lazily (inside the function) so ``etl.spec`` stays
    import-cycle-free: supervisor -> agents -> etl.base -> etl.spec.
    Resolves every domain in AGENT_REGISTRY, so callers pay the full lazy-
    registry import cost — appropriate for scheduler/monitor processes that
    need the whole roster, not for per-dispatch paths.
    """
    from infra_brain.supervisor import AGENT_REGISTRY  # noqa: PLC0415

    return {
        domain: cls.spec
        for domain, cls in AGENT_REGISTRY.items()
        if getattr(cls, "spec", None) is not None
    }


def retired_domains() -> set[str]:
    """Domains that are switched off by standing decision, right now.

    ``AgentSpec.retired=True`` declares the decision in code; the
    ``collection_revived_domains`` setting is the escape hatch that turns one
    back ON without a code change (it is a normal ``Settings`` field, so it is
    settable from env AND live-editable via the ``runtime_config`` table that
    ``get_settings()`` layers on top — no redeploy needed either way).

    WHICH DIRECTION THE OVERRIDE WINS: the override only ever turns a collector
    ON. Listing a domain in ``collection_revived_domains`` un-retires it;
    nothing in that setting can retire a live domain. That asymmetry is
    deliberate — turning a collector OFF already has two purpose-built levers
    (``collection_disabled_domains`` for a static skip, ``dispatchable__<domain>``
    for a live pause), and a second way to do it would leave an operator
    guessing which one is in force. Retirement is the one direction that
    previously required editing a critical file, so that is the direction this
    override exists for.

    FAIL-SAFE, NOT FAIL-OPEN. If the settings read raises (unmigrated table,
    unreachable DB), a retired domain STAYS retired. The alternative would let
    a transient DB blip silently start dispatching a collector against an
    upstream system that does not exist — turning a config problem into live
    traffic. Nothing here is on a hot path: every caller either caches the
    result or runs once at scheduler start.

    REVIVING A HOST-BEARING DOMAIN IS NOT JUST A SETTING (P5). ``windows``,
    ``cloud`` and ``k8s`` each feed one of ``host_reconcile._SOURCE_KEYS``'
    identity legs, and ``host_reconcile`` no longer writes ``IS_SAME_AS`` — the
    identity resolver (``graph_phase3.resolve_entities``) is the sole writer, and
    it only ever loads node types listed in ``graph_phase3.HOST_NODE_TYPES``.
    So a revived host-bearing domain with no ``NodeSpec(is_host_identity=True)``
    and no entry in that tuple produces hosts that are INVISIBLE to cross-source
    identity: not merged, not queued for a human, silently absent — the exact
    failure this repo just spent P5 closing for ``linux``. Each such spec carries
    the obligation as a "P5 REVIVAL OBLIGATION" comment next to its
    ``retired=True``; ``tests/test_p5_issameas_resolver_coverage.py`` enforces it
    for every domain that is actually live.
    """
    # ``is True``, not truthiness: this runs against whatever AGENT_REGISTRY
    # currently holds, and a MagicMock stand-in (what most dispatch tests patch
    # in) answers every getattr with a truthy Mock — under a plain truth test
    # that silently retires every domain in the fake registry.
    declared = {
        domain for domain, spec in agent_specs().items() if getattr(spec, "retired", False) is True
    }
    if not declared:
        return set()
    try:
        from infra_brain.config import get_settings  # noqa: PLC0415

        raw = get_settings().collection_revived_domains or ""
    except Exception:  # noqa: BLE001 — fail-safe: stay retired, see docstring
        return declared
    revived = {d.strip() for d in raw.split(",") if d.strip()}
    return declared - revived


def is_retired(domain: str) -> bool:
    """Is this ONE domain currently retired? Same rule as ``retired_domains()``.

    Exists because ``retired_domains()`` goes through ``agent_specs()``, which
    resolves every domain in AGENT_REGISTRY and so imports all ~46 agent
    modules. That is right for the scheduler and the roster (they need the
    whole roster anyway), and wrong for the per-dispatch path: ``dispatch()``
    needs exactly one domain's class per call, and paying the full import cost
    on every dispatch would defeat the lazy registry that supervisor.py's
    ``_LazyAgentRegistry`` exists to provide. Looks up the single class instead
    — the same trick ``collect_timeout_override_for_domain`` already uses.

    Unknown domains are not retired (the caller's own unknown-domain check owns
    that case). The settings read is skipped entirely unless the spec actually
    declares retirement, so the common path costs one dict lookup.
    """
    from infra_brain.supervisor import AGENT_REGISTRY  # noqa: PLC0415

    cls = AGENT_REGISTRY.get(domain)
    spec = getattr(cls, "spec", None) if cls is not None else None
    if getattr(spec, "retired", False) is not True:
        return False
    try:
        from infra_brain.config import get_settings  # noqa: PLC0415

        raw = get_settings().collection_revived_domains or ""
    except Exception:  # noqa: BLE001 — fail-safe: stay retired, see retired_domains()
        return True
    return domain not in {d.strip() for d in raw.split(",") if d.strip()}


def max_staleness_by_domain() -> dict[str, timedelta]:
    """Domain -> max_staleness for every spec that declares one.

    Replaces ``fleet_health._DOMAIN_MAX_AGE`` and backs the derived
    ``freshness.DOMAIN_EXPECTED_MAX_AGE`` mapping.

    Retired domains are excluded, which is what stops the freshness monitor
    and fleet_health alerting on them: a collector that is switched off on
    purpose cannot meaningfully be "stale", and its last successful run recedes
    forever, so the alert could never be cleared by any action except
    configuring an integration nobody asked for.
    """
    retired = retired_domains()
    return {
        domain: spec.max_staleness
        for domain, spec in agent_specs().items()
        if spec.max_staleness is not None and domain not in retired
    }


def schedule_by_domain() -> dict[str, str]:
    """Domain -> cron schedule for every spec that declares one.

    Replaces ``coverage.DEFAULT_SCHEDULES`` as the expected-cadence lookup.
    Retired domains are excluded — they have no expected cadence, so coverage
    must not score them against one. The spec keeps its cron string so
    re-enabling restores the original cadence; this view just does not report it.
    """
    retired = retired_domains()
    return {
        domain: spec.schedule
        for domain, spec in agent_specs().items()
        if spec.schedule is not None and domain not in retired
    }


def graph_emitting_domains() -> set[str]:
    """Domains that declare ANY graph contribution.

    The engine iterates this rather than every spec, so the ~30 domains that
    declare nothing cost nothing. It is also the honest measure of how far the
    migration has actually got — assert on it in tests rather than trusting a
    phase table in a design doc.
    """
    return {
        domain for domain, spec in agent_specs().items() if spec.emits_nodes or spec.emits_edges
    }


def identity_keys_by_domain() -> dict[str, tuple[str, ...]]:
    """Domain -> the attribute names that source uses to name real-world things.

    Declaration only. The cross-source resolver still uses ``host_reconcile``'s
    hardcoded per-source legs; switching it over to read this is P2.
    """
    return {
        domain: spec.identity_keys for domain, spec in agent_specs().items() if spec.identity_keys
    }


def declared_host_identity_node_types() -> dict[str, str]:
    """Node type -> declaring domain, for every ``NodeSpec(is_host_identity=True)``.

    The DECLARATION side of the P5 resolver-coverage guard. The consumption side
    is ``graph_phase3.HOST_NODE_TYPES``, an explicit tuple that deliberately does
    NOT call this — see ``NodeSpec.is_host_identity`` for why the resolver must
    not import the whole collector registry to decide who it is allowed to look
    at. ``tests/test_p5_issameas_resolver_coverage.py`` compares the two, so a
    collector that declares a host node without widening the tuple fails a test
    instead of being silently invisible to entity resolution.

    Retired / unregistered domains contribute nothing here, because
    ``agent_specs()`` reads ``AGENT_REGISTRY`` — which is exactly right: the
    guard should not demand a resolver entry for a machine class that cannot
    currently produce a single row. The revival note on each such spec carries
    that obligation forward instead.
    """
    out: dict[str, str] = {}
    for domain, spec in agent_specs().items():
        for node in spec.emits_nodes:
            if node.is_host_identity:
                out[node.type] = domain
    return out


def collect_timeout_override_for_domain(domain: str) -> int | None:
    """Return *domain*'s per-domain collect-timeout override, or None.

    None means "no override — use the global ``settings.collect_timeout_seconds``".
    This is the SINGLE source of the per-domain override, consulted by the two
    places whose timeouts MUST agree (TRK-117 gotcha #2):

      * ``BaseAgent._call_with_timeout`` — the actual collect() guard (which
        reads ``type(self).spec`` directly, the same value this returns), and
      * ``dedup.default_ttl_seconds`` — the Redis dedup-lock TTL, which applies
        this override on top of its own global-settings read.

    If these disagreed, a per-domain timeout bump (graph_maintenance: 1200s)
    would let the lock expire at the global default (300+120s) while collect()
    kept running, admitting a duplicate overlapping dispatch.

    Looks the domain up in AGENT_REGISTRY (lazy import — same pattern as
    ``agent_specs()`` — so ``etl.spec`` stays import-cycle-free). Unknown
    domains return None (global default applies).
    """
    from infra_brain.supervisor import AGENT_REGISTRY  # noqa: PLC0415

    cls = AGENT_REGISTRY.get(domain)
    spec = getattr(cls, "spec", None) if cls is not None else None
    return getattr(spec, "collect_timeout_seconds", None) if spec is not None else None


# Phase 2 Task 1 (orchestration v2.1 groundwork): the sweep graph's fixed
# tier execution order. REPORTER and ON_DEMAND are deliberately excluded —
# they run outside the graph (reporting-only / explicitly-dispatched agents).
TIER_ORDER: tuple[Tier, ...] = (Tier.COLLECTOR, Tier.RECONCILER, Tier.REASONER)


def sweep_members() -> dict[Tier, list[str]]:
    """Domains grouped by tier, for the tiers the sweep graph actually runs.

    Only tiers in ``TIER_ORDER`` are included (REPORTER/ON_DEMAND domains —
    fleet_health, learning_feedback, coverage, discovery, query,
    inventory_mr — are never sweep members), and only specs with
    ``dispatchable=True`` that are not ``retired``.

    Excluding retired here is the ONLY change the sweep graph needs: ``graph.py``
    derives its whole topology from this function, so a retired domain is never
    given a node and never reaches ``_plan_collectors``'s ``known`` list. It
    drops out of the topology rather than being added and then filtered, which
    is why it produces no per-sweep "skipped" status either.

    Decision (spec deviation, recorded in docs/TRACKER.md): inventory_reconcile
    IS a sweep member. Its shipped tier is RECONCILER, and reconciling
    inventory belongs after collection in the sweep's execution order; the
    original spec's "outside the graph" list predates the tier assignments
    and is superseded here.
    """
    members: dict[Tier, list[str]] = {tier: [] for tier in TIER_ORDER}
    retired = retired_domains()
    for domain, spec in agent_specs().items():
        if spec.tier not in members or not spec.dispatchable or domain in retired:
            continue
        members[spec.tier].append(domain)
    for domains in members.values():
        domains.sort()
    return members
