"""The one rule that decides whether a data source appears in the dashboard UI.

WHY THIS MODULE EXISTS
----------------------
The dashboard shipped with a hardcoded sidebar listing every source the
*enterprise* deployment this codebase grew out of ever had — Rapid7, vSphere,
Octopus, AWS, Kubernetes, Windows. In this (homelab) environment those upstream
systems do not exist and are never going to, which is exactly what
``AgentSpec.retired=True`` already says in code. But nothing connected that
declaration to the UI, so an operator logging in was offered six integrations
they cannot configure and one (``identity``) whose real, collected data was
indistinguishable from them.

This module is that connection, and it is deliberately ONE function so there is
exactly one place to read, test, and change the policy. It is pure — no DB, no
settings, no imports from ``db``/``config`` — so the tests can exercise it with
fictional domain names and cannot rot when the real roster changes.

THE RULE
--------
Two inputs, four cases:

    +------------------+--------------+--------------------------------------+
    | collector        | rows in DB   | verdict                              |
    +==================+==============+======================================+
    | live             | any (incl. 0)| VISIBLE                              |
    | retired          | > 0          | HISTORICAL                           |
    | retired          | 0            | HIDDEN                               |
    +------------------+--------------+--------------------------------------+

* **live → always VISIBLE, even at zero rows.** A live collector with no rows
  is a collector that has not run yet, or ran and legitimately found nothing.
  Hiding it would hide the very thing an operator needs to see to notice that a
  source they *do* intend to use is not producing data. Emptiness is a state to
  render, not a reason to disappear.

* **retired + data → HISTORICAL, not hidden.** ``identity`` is the live example:
  the Okta collector is switched off by standing decision, and 53 real resource
  rows it collected are still in the database. Those rows are true. Hiding the
  only route that reaches them would make the UI assert, by omission, that data
  which exists does not — the UI would be lying. It is shown, and marked
  read-only/historical so nobody mistakes a frozen snapshot for a live feed.

* **retired + zero rows → HIDDEN.** Nothing to show and nothing coming. This is
  the Rapid7/vSphere/Cloud/K8s/Octopus/Windows case, and hiding it is the whole
  point of the exercise.

WHAT IS DELIBERATELY *NOT* AN INPUT
-----------------------------------
**Configuration state.** Whether ``RAPID7_API_KEY`` is set does not appear
above. A retired collector stays hidden even if someone leaves credentials in
the environment, because retirement is the standing decision and configuration
is not the lever that reverses it — ``COLLECTION_REVIVED_DOMAINS`` is (see
``etl.spec.retired_domains``). Feeding config into this rule would let a stray
env var resurrect a page for a system that is not there. Configuration is still
*reported* by the sources endpoint, because "live but unconfigured" is useful
diagnosis; it just does not decide visibility.

**Any hardcoded domain list.** Callers derive ``retired`` from
``etl.spec.agent_specs()`` and ``row_count`` from the database. Add a collector
tomorrow and it appears in the UI with no edit here, which is the property that
makes this worth having at all.
"""

from __future__ import annotations

import enum

__all__ = ["SourceVisibility", "source_visibility", "visibility_reason"]


class SourceVisibility(str, enum.Enum):
    """UI verdict for one data source. ``str`` mixin so it serialises as its value."""

    #: Live collector — full, normal navigation entry.
    VISIBLE = "visible"
    #: Retired collector that still holds real rows — reachable, marked read-only.
    HISTORICAL = "historical"
    #: Retired collector with nothing to show — absent from the UI entirely.
    HIDDEN = "hidden"


def source_visibility(*, retired: bool, row_count: int) -> SourceVisibility:
    """Decide whether a source appears in the dashboard. See the module docstring.

    Args:
        retired: Is this collector switched off by standing decision *right
            now*? Callers pass ``domain in etl.spec.retired_domains()``, which
            already folds in the ``COLLECTION_REVIVED_DOMAINS`` override — so a
            revived domain arrives here as ``retired=False`` and is simply
            VISIBLE, with no special case needed on this side.
        row_count: How many ``resources`` rows this domain owns. Negative values
            are treated as zero rather than rejected; a bad count must not be
            able to turn a hidden source back on.

    Returns:
        The verdict. Never raises.
    """
    if not retired:
        return SourceVisibility.VISIBLE
    return SourceVisibility.HISTORICAL if row_count > 0 else SourceVisibility.HIDDEN


def visibility_reason(*, retired: bool, row_count: int) -> str:
    """One human sentence explaining the verdict, for the UI and for triage.

    Kept next to the rule rather than in the router so the explanation cannot
    drift away from the decision it explains.
    """
    verdict = source_visibility(retired=retired, row_count=row_count)
    if verdict is SourceVisibility.VISIBLE:
        return (
            "Live collector — always shown, including at zero rows so a source "
            "that is not producing data stays visible."
        )
    if verdict is SourceVisibility.HISTORICAL:
        return (
            f"Collector is retired but {row_count} collected row(s) remain — shown "
            "read-only as historical data, never hidden."
        )
    return "Collector is retired and holds no data — hidden; nothing to show and nothing coming."
