"""TRK-088 (VULNERABLE_TO property-shape unification) — RETIRED IN P5.

This module's entire subject was a TWO-WRITER hazard, and P5 deleted one of the
two writers. Nothing here can be ported; the premise is gone.

WHAT IT TESTED. Two independent emitters wrote VULNERABLE_TO edges from the
same (VulnQueueItem, R7VulnCve, R7Vulnerability) rows into
``resource_relationships``:

  * ``vuln.VulnAgent._write_graph_edges``
  * ``graph_maintenance.GraphMaintenanceAgent._populate_typed_relationships``

The shared upsert (``db/relationships._UPSERT_SQL``) does
``properties = EXCLUDED.properties`` — a FULL overwrite of the JSONB bag on the
natural key (from, to, type). Before TRK-088, vuln.py emitted ``{cve_id,
severity}`` while graph_maintenance emitted the 4-key superset ``{cve_id,
severity, exploits, malware_kits}``, so whichever writer landed LAST silently
dropped the other's fields. TRK-088 unified vuln.py onto the superset, and the
four tests here pinned that: the superset shape, byte-identical key sets across
both writers, and an end-to-end DO-UPDATE upsert in BOTH orderings.

WHY IT IS GONE. P5 deleted ``VulnAgent._write_graph_edges`` along with the
``resource_relationships`` store it was the only writer into (see its epitaph in
``agents/vuln.py``: Rapid7 is not deployed, the collector is retired, zero live
rows ever). With one writer removed there is no ordering to test, no second key
set to agree with, and no clobber to regress. Three of the four tests compared
the two writers directly; the fourth drove the deleted method.

BOTH writers are gone on the integrated P5 branch. This wave also deleted
``GraphMaintenanceAgent._populate_typed_relationships`` (the other half of the
pair — see ``tests/agents/test_graph_maintenance_legacy_writers_gone.py``), so
there is no surviving VULNERABLE_TO emitter of any shape anywhere: not two
writers, not one. The 4-key superset shape is pinned by nothing because nothing
produces it.

DO NOT RESTORE THESE TESTS. They exercise deleted code. If VULNERABLE_TO is ever
re-established — it would take Rapid7 actually being deployed — it comes back as
a COLLECTOR DECLARATION (``spec.emits_edges``) materialised into ``graph_edges``,
not as a hand-written emitter into the dropped store. ``graph_edges`` is
bitemporal and single-authority by construction, so the last-writer-wins
overwrite hazard TRK-088 existed to close cannot recur there in the same shape;
a new declaration needs its own test against the declaration, not this one.

The file is kept (rather than deleted outright) as a discoverable gravestone:
TRK-088 is referenced from the tracker and from ``agents/vuln.py``, and a reader
following either pointer should land on this explanation rather than on a
missing path.
"""
