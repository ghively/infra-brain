"""Shared page-envelope marker and pagination helper.

Task 5.2 — Phase 5 contract rework.

## Design choice: plain mixin, not Generic[T]

We chose a plain marker mixin (``PageEnvelope(BaseModel)``) over a true
``Generic[T]`` approach for one reason: all existing ``response_model=`` usages
in the router files reference concrete class names (``CveListOut``,
``FleetAssetsOut``, etc.).  A generic would require changing every call-site to
``response_model=PageOut[CveListItemOut]`` across multiple router files plus
rewriting every constructor call — a large, risk-introducing churn with no
functional benefit.  The mixin approach:

  * Requires zero router changes (``response_model=CveListOut`` still works).
  * Allows each subclass to override ``items`` with its proper typed list —
    Pydantic v2 supports field override in subclasses.
  * Enables ``issubclass(response_model, PageEnvelope)`` detection in
    ``_degraded_body_for``, fixing the 4 non-``*PageOut``-suffixed classes.

Task 5.4 (complete): ``SoftwareInventoryOut`` now inherits ``PageEnvelope``
and uses ``items`` — the ``aggregated`` / ``detail`` split has been collapsed.
"""

from __future__ import annotations

from pydantic import BaseModel


class PageEnvelope(BaseModel):
    """Marker base class for every paged collection response.

    Concrete subclasses override ``items`` with a properly-typed list::

        class ResourcePageOut(PageEnvelope):
            items: list[ResourceOut]

    The four untyped fields defined here are the minimum contract that
    ``_degraded_body_for`` and frontend client code may rely on.  Subclasses
    that carry extra sidecar fields (e.g. ``by_severity``, ``header``,
    ``summary``) must give those fields defaults so the degraded envelope
    ``{"items": [], "total": 0, "limit": 0, "offset": 0, "_degraded": True}``
    deserializes without validation errors.
    """

    items: list
    total: int
    limit: int
    offset: int


# M-3: sane ceiling for a single page. Chosen to match the highest cap
# already in use by an individually-clamped call site (hosts.py's
# ``/resources`` route clamps to 1000) so this shared clamp doesn't tighten
# any legitimate existing caller — it only closes the gap for the call sites
# that were applying the caller-supplied limit/offset verbatim.
MAX_PAGE_LIMIT = 1000


def paginate(query, limit: int, offset: int) -> tuple[list, int]:
    """Count-then-slice helper for SQLAlchemy queries.

    Computes ``total`` via ``query.count()`` **before** applying
    ``offset``/``limit``, so the caller always gets the true DB-wide count
    rather than the capped page size.

    ``limit`` and ``offset`` are clamped here (M-3) rather than trusted
    verbatim: an unbounded ``?limit=`` on an unbounded table (e.g. the audit
    log) would otherwise materialize the whole table into Python in one
    request, and a negative ``?offset=`` would otherwise reach Postgres as a
    literal negative ``OFFSET``, which raises and — unless the caller
    happens to catch it — surfaces as a masked empty 200. Individual routers
    that already clamp more tightly for their own reasons (e.g. a 200-row
    domain-specific cap) are unaffected: this clamp only ever *widens* the
    accepted range down to ``MAX_PAGE_LIMIT``, it never loosens a stricter
    per-route cap that ran before calling in here.

    Args:
        query:  An un-sliced SQLAlchemy ``Query`` object (pre-filter,
                pre-order is fine — just do not call ``.offset()`` or
                ``.limit()`` before passing it in).
        limit:  Maximum rows to return for this page. Clamped to
                ``[1, MAX_PAGE_LIMIT]``.
        offset: Row offset for pagination. Clamped to ``>= 0``.

    Returns:
        ``(items, total)`` where ``items`` is the sliced page (a plain Python
        list from ``.all()``) and ``total`` is the full count of matching rows.

    Example::

        with get_session() as s:
            q = s.query(Resource).filter(Resource.domain == domain)
            items, total = paginate(q.order_by(Resource.last_seen.desc()),
                                    limit=limit, offset=offset)
            return ResourcePageOut(items=items, total=total,
                                   limit=limit, offset=offset)
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    offset = max(0, offset)
    total: int = query.count()
    items: list = query.offset(offset).limit(limit).all()
    return items, total
