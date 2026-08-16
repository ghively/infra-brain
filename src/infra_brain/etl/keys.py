"""Named key normalisers for declarative graph edges (design doc §4, P0).

An ``EdgeSpec`` joins two node sets by comparing a key taken from each side.
Real sources rarely spell the same thing the same way, so the comparison needs
a normalisation step — and that step has to be part of the DECLARATION, not
hidden in engine code, or the engine grows per-source special cases and we are
back to the 6,367-line hand-maintained deriver the contract exists to replace.

Normalisers are referenced BY NAME (``key_normalizer="host"``) rather than by
passing a callable into ``AgentSpec``, for three reasons:

* ``AgentSpec`` is a frozen dataclass that is compared, hashed and logged;
  a bare function object in it is neither stable across processes nor
  readable in a log line.
* ``etl.spec`` is imported by ``etl.base`` and therefore by every agent. It
  must stay cheap and import-cycle-free; a name is free, a callable would
  drag whatever module defines it into that hot path.
* A closed, named vocabulary is reviewable. "This edge joins on ``host``" is
  a claim someone can check; an inline lambda is not.

This module is the ONE home for both the vocabulary and the callables, so
``etl.spec`` (which validates a declaration) and ``graph_engine`` (which
applies it) cannot drift apart.

Adding a normaliser is a deliberate, reviewable central edit — the only one
the contract still requires, and only when a genuinely new *kind* of key
appears. Adding a SOURCE requires none.
"""

from __future__ import annotations

from typing import Callable, Mapping

from infra_brain.tools.hostmatch import graph_match_key


def _exact(value: str) -> str:
    """No normalisation — byte-for-byte equality, after None-guarding."""
    return value or ""


def _lower(value: str) -> str:
    return (value or "").strip().lower()


#: name -> normaliser. Every callable must be total on ``str | None`` and
#: return ``""`` for "no usable key" (which the engine reads as "emit
#: nothing", never as "match everything").
KEY_NORMALIZERS: Mapping[str, Callable[[str], str]] = {
    "exact": _exact,
    "lower": _lower,
    # normalize_host() + separator folding: the homelab's manifest spells
    # hosts with hyphens and its Ansible inventory with underscores.
    "host": graph_match_key,
}

KEY_NORMALIZER_NAMES: frozenset[str] = frozenset(KEY_NORMALIZERS)


__all__ = ["KEY_NORMALIZERS", "KEY_NORMALIZER_NAMES"]
