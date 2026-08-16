"""Cloud / Kubernetes / Network relational model.

Bucket: CloudResource, K8sNode, K8sPod, K8sDeployment, NetDevice,
        NetDiscoveryHost, NetDiscoveryService.

Design notes (preserved from original models.py):
- The cloud, k8s, and net collectors previously emitted only generic Resource
  rows + JSONB. These tables give each a pragmatic relational shape (typed
  columns + a JSONB ``details`` overflow), keyed on a natural key, with a
  nullable ``resource_id`` FK back to the canonical Resource row.
- The collectors stay idle (AWS off / no clusters / SNMP empty) until enabled,
  so these ship "ready for when enabled".
- NetDiscoveryHost is produced by the netdiscovery agent (active scanning).
- NetDiscoveryService: natural key (host_id, port, proto).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import JSONB, Base, _now, _uuid
from .core import ZONE_CORPORATE, _ZONE_SERVER_DEFAULT


class CloudResource(Base):
    """One AWS resource (ec2_instance / vpc / security_group), keyed (provider, cloud_id)."""

    __tablename__ = "cloud_resources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    cloud_type: Mapped[str] = mapped_column(String(64))
    cloud_id: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(256))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "cloud_id", name="uq_cloud_resource_natkey"),)


class K8sNode(Base):
    """A Kubernetes node, keyed (cluster, name). ``cluster`` is nullable (single in-context cluster)."""

    __tablename__ = "k8s_nodes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    cluster: Mapped[str | None] = mapped_column(String(256), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    roles: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    kubelet_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_image: Mapped[str | None] = mapped_column(String(256), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("cluster", "name", name="uq_k8s_node_natkey"),)


class K8sPod(Base):
    """A Kubernetes pod, keyed (cluster, namespace, name)."""

    __tablename__ = "k8s_pods"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    cluster: Mapped[str | None] = mapped_column(String(256), nullable=True)
    namespace: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(256))
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    node_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("cluster", "namespace", "name", name="uq_k8s_pod_natkey"),)


class K8sDeployment(Base):
    """A Kubernetes deployment, keyed (cluster, namespace, name)."""

    __tablename__ = "k8s_deployments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    cluster: Mapped[str | None] = mapped_column(String(256), nullable=True)
    namespace: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(256))
    replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ready: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        UniqueConstraint("cluster", "namespace", "name", name="uq_k8s_deployment_natkey"),
    )


class NetDevice(Base):
    """A network device discovered via SNMP, keyed on its target ``ip``."""

    __tablename__ = "net_devices"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    ip: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    sysname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    descr: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    uptime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("ip", name="uq_net_device_natkey"),)


class NetDiscoveryHost(Base):
    """Per-IP host record produced by the netdiscovery agent.

    Natural key: ``ip`` — every unique IP address gets exactly one row.  The
    row is upserted on every sweep so ``last_seen`` advances and all
    classification flags stay current without duplicates.
    """

    __tablename__ = "net_discovery_hosts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # ip is the natural key — unique constraint enforced here (creates an implicit index).
    ip: Mapped[str] = mapped_column(String(45), nullable=False, unique=True)
    mac: Mapped[str | None] = mapped_column(String(17), nullable=True)
    mac_vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    responded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Tier that produced the initial discovery row ("passive" | "tier1" | "tier2").
    discovery_tier: Mapped[str] = mapped_column(String(16), default="passive")
    is_fragile: Mapped[bool] = mapped_column(Boolean, default=False)
    # Classification — populated by classify phase.
    is_known: Mapped[bool] = mapped_column(Boolean, default=False)
    is_shadow_it: Mapped[bool] = mapped_column(Boolean, default=False)
    # Threat level: none / low / med / high
    threat_level: Mapped[str] = mapped_column(String(8), default="none")
    # Back-reference to the generic Resource row (populated after upsert).
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    zone: Mapped[str] = mapped_column(
        String(32), default=ZONE_CORPORATE, server_default=_ZONE_SERVER_DEFAULT
    )
    services: Mapped[list["NetDiscoveryService"]] = relationship(back_populates="host")


class NetDiscoveryService(Base):
    """Per-port service record linked to a discovered host.

    Natural key: ``(host_id, port, proto)``.  Re-upserted on every Tier 2 scan
    so ``last_seen`` advances and banner/fingerprint data stays current.
    """

    __tablename__ = "net_discovery_services"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # host_id is indexed via __table_args__ below — do not also set index=True here
    # to avoid a duplicate-index error on SQLite (which is what the tests use).
    host_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("net_discovery_hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    proto: Mapped[str] = mapped_column(String(8), nullable=False)
    service: Mapped[str | None] = mapped_column(String(128), nullable=True)
    banner: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Classification flags.
    is_dangerous: Mapped[bool] = mapped_column(Boolean, default=False)
    is_suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    host: Mapped["NetDiscoveryHost"] = relationship(back_populates="services")

    __table_args__ = (
        UniqueConstraint(
            "host_id", "port", "proto", name="uq_net_discovery_services_host_port_proto"
        ),
        Index("ix_net_discovery_services_host_id", "host_id"),
    )


# cloud / k8s / net relational model (MR5): natural-key lookups back the upserts
Index("ix_cloud_resources_natkey", CloudResource.provider, CloudResource.cloud_id)
Index("ix_k8s_nodes_natkey", K8sNode.cluster, K8sNode.name)
Index("ix_k8s_pods_natkey", K8sPod.cluster, K8sPod.namespace, K8sPod.name)
Index(
    "ix_k8s_deployments_natkey", K8sDeployment.cluster, K8sDeployment.namespace, K8sDeployment.name
)
Index("ix_net_devices_natkey", NetDevice.ip)


__all__ = [
    "CloudResource",
    "K8sDeployment",
    "K8sNode",
    "K8sPod",
    "NetDevice",
    "NetDiscoveryHost",
    "NetDiscoveryService",
]
