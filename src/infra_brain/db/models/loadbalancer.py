"""Load balancer / reverse proxy / CDN relational model (GitLab issue #100).

Bucket: ``LbInstance``, ``LbPool``, ``LbPoolMember``, ``LbVirtualServer``.

Covers F5 (iControl REST), nginx Plus (API), HAProxy (stats socket/page), and
Cloudflare (API) — all read-only via GET-only admin/management APIs (see
``infra_brain.tools.loadbalancer`` and ``infra_brain.agents.loadbalancer``).
Currently a total blank in the topology graph even though
``resource_relationships`` already models multi-hop infra topology; these
tables plus the new ``ROUTES_TO`` / ``MEMBER_OF_POOL`` / ``TERMINATES_TLS_FOR``
edge types (see ``infra_brain.db.relationships``) close that gap.

Shape (per the issue's design sketch):
  - ``LbInstance``       — one LB/proxy/CDN device or account (F5 box, nginx
                           Plus host, HAProxy host, Cloudflare zone).
  - ``LbPool``            — a backend pool ("pool" / "upstream" / "backend").
  - ``LbPoolMember``      — one member of a pool (a real backend
                           host:port — the LB's health-check target).
  - ``LbVirtualServer``   — a virtual server / listener that ROUTES_TO a pool
                           and, when it terminates TLS, carries the
                           certificate thumbprint for TERMINATES_TLS_FOR.

Every table keeps a nullable ``resource_id`` FK back to the canonical
``resources.id`` row (populated after the generic Resource upsert), mirroring
the ``cloud_k8s_net`` / ``net_devices`` precedent — plus a ``details`` JSONB
overflow for anything not worth a typed column.
"""

import uuid
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONB, _uuid


class LbInstance(Base):
    """One load-balancer / reverse-proxy / CDN instance.

    Natural key: ``(vendor, name)`` — ``vendor`` is one of "f5" | "nginx" |
    "haproxy" | "cloudflare"; ``name`` is the management hostname (F5/nginx/
    HAProxy) or the Cloudflare zone name.
    """

    __tablename__ = "lb_instances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vendor: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    management_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    pools: Mapped[list["LbPool"]] = relationship(back_populates="instance")
    virtual_servers: Mapped[list["LbVirtualServer"]] = relationship(back_populates="instance")

    __table_args__ = (UniqueConstraint("vendor", "name", name="uq_lb_instance_natkey"),)


class LbPool(Base):
    """A backend pool ("pool" / "upstream" / "backend"), scoped to one instance.

    Natural key: ``(instance_id, name)``.
    """

    __tablename__ = "lb_pools"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lb_instances.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256))
    #  health-check config: monitor/probe type + interval (e.g. "http" / "tcp").
    monitor_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    monitor_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    instance: Mapped["LbInstance"] = relationship(back_populates="pools")
    members: Mapped[list["LbPoolMember"]] = relationship(back_populates="pool")

    __table_args__ = (UniqueConstraint("instance_id", "name", name="uq_lb_pool_natkey"),)


class LbPoolMember(Base):
    """One backend member (host:port) of a pool — the LB's health-check target.

    Natural key: ``(pool_id, address, port)``. ``resource_id`` links to the
    backend host's own Resource row when it resolves (best-effort name/IP
    match against known host resources — not always resolvable, so nullable).
    """

    __tablename__ = "lb_pool_members"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lb_pools.id", ondelete="CASCADE"), nullable=False
    )
    address: Mapped[str] = mapped_column(String(256))
    port: Mapped[int] = mapped_column(Integer)
    #  up | down | disabled | unknown — collector-reported health-check state.
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    weight: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    pool: Mapped["LbPool"] = relationship(back_populates="members")

    __table_args__ = (
        UniqueConstraint("pool_id", "address", "port", name="uq_lb_pool_member_natkey"),
    )


class LbVirtualServer(Base):
    """A virtual server / listener that fronts traffic for a pool.

    Natural key: ``(instance_id, name)``. ``pool_id`` is the pool it
    ROUTES_TO (nullable — some listeners have no default pool bound).
    ``tls_certificate_thumbprint`` — when set, backs the
    ``TERMINATES_TLS_FOR`` edge to the matching ``security/certificate``
    convergence node (see ``HasCertificateProps`` — same thumbprint key).
    """

    __tablename__ = "lb_virtual_servers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lb_instances.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("lb_pools.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    address: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    protocol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    tls_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tls_certificate_thumbprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    instance: Mapped["LbInstance"] = relationship(back_populates="virtual_servers")

    __table_args__ = (UniqueConstraint("instance_id", "name", name="uq_lb_virtual_server_natkey"),)


# lb relational model: natural-key lookups back the upserts.
Index("ix_lb_instances_natkey", LbInstance.vendor, LbInstance.name)
Index("ix_lb_pools_natkey", LbPool.instance_id, LbPool.name)
Index("ix_lb_pool_members_natkey", LbPoolMember.pool_id, LbPoolMember.address, LbPoolMember.port)
Index("ix_lb_virtual_servers_natkey", LbVirtualServer.instance_id, LbVirtualServer.name)


__all__ = [
    "LbInstance",
    "LbPool",
    "LbPoolMember",
    "LbVirtualServer",
]
