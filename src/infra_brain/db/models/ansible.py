"""IaC / CI-CD: GitLab + parsed infrastructure-as-code relational model.

Bucket: GitlabProject, IacFile, CiPipelineRun, ComposeService,
        K8sManifestResource, TerraformResource, AnsibleInventoryGroup,
        AnsibleInventoryHost, AnsiblePlaybookPlay, CiSchedule.

Design notes (preserved from original models.py):
- ``gitlab_projects`` and ``ci_pipeline_runs`` key off the upstream GitLab
  integer ids (unique).
- ``iac_files`` are the parsed IaC files in a project; their children (compose
  services, k8s resources, terraform resources, ansible inventory groups/hosts,
  playbook plays) hang off ``iac_files.id``.
- A JSONB ``details`` column on each primary table carries nested/rarely-queried
  extras so the typed columns stay focused on what we query and relate.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONB, _now, _uuid


class GitlabProject(Base):
    __tablename__ = "gitlab_projects"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    gitlab_project_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    path_with_namespace: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    default_branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    visibility: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pipeline_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_pipeline_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_pipeline_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Link to iac_files / ci_pipeline_runs is by the upstream gitlab_project_id
    # column (not a DB FK), so it is queried directly rather than via an ORM
    # relationship — keeping the mapper light and avoiding a non-FK join.


class IacFile(Base):
    __tablename__ = "iac_files"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    gitlab_project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    path: Mapped[str] = mapped_column(String(512))
    # gitlab_ci | playbook | inventory | requirements | compose | k8s_manifest | terraform
    file_type: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    compose_services: Mapped[list["ComposeService"]] = relationship(back_populates="iac_file")
    k8s_resources: Mapped[list["K8sManifestResource"]] = relationship(back_populates="iac_file")
    terraform_resources: Mapped[list["TerraformResource"]] = relationship(back_populates="iac_file")
    inventory_groups: Mapped[list["AnsibleInventoryGroup"]] = relationship(
        back_populates="iac_file"
    )
    plays: Mapped[list["AnsiblePlaybookPlay"]] = relationship(back_populates="iac_file")
    __table_args__ = (
        UniqueConstraint(
            "gitlab_project_id", "path", "ref", "file_type", name="uq_iac_file_natkey"
        ),
    )


class CiPipelineRun(Base):
    __tablename__ = "ci_pipeline_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    gitlab_project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, index=True)
    ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    web_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    __table_args__ = (
        UniqueConstraint("gitlab_project_id", "pipeline_id", name="uq_ci_pipeline_run_natkey"),
    )


class ComposeService(Base):
    __tablename__ = "compose_services"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    iac_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("iac_files.id"), index=True)
    service_name: Mapped[str] = mapped_column(String(256))
    image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ports: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    iac_file: Mapped["IacFile"] = relationship(back_populates="compose_services")
    __table_args__ = (
        UniqueConstraint("iac_file_id", "service_name", name="uq_compose_service_natkey"),
    )


class K8sManifestResource(Base):
    __tablename__ = "k8s_manifest_resources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    iac_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("iac_files.id"), index=True)
    kind: Mapped[str] = mapped_column(String(128))
    api_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    namespace: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    iac_file: Mapped["IacFile"] = relationship(back_populates="k8s_resources")
    __table_args__ = (
        UniqueConstraint(
            "iac_file_id", "kind", "name", "namespace", name="uq_k8s_manifest_resource_natkey"
        ),
    )


class TerraformResource(Base):
    __tablename__ = "terraform_resources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    iac_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("iac_files.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(128))
    resource_name: Mapped[str] = mapped_column(String(256))
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    iac_file: Mapped["IacFile"] = relationship(back_populates="terraform_resources")
    __table_args__ = (
        UniqueConstraint(
            "iac_file_id", "resource_type", "resource_name", name="uq_terraform_resource_natkey"
        ),
    )


class AnsibleInventoryGroup(Base):
    __tablename__ = "ansible_inventory_groups"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    iac_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("iac_files.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    iac_file: Mapped["IacFile"] = relationship(back_populates="inventory_groups")
    hosts: Mapped[list["AnsibleInventoryHost"]] = relationship(back_populates="group")
    __table_args__ = (
        UniqueConstraint("iac_file_id", "name", name="uq_ansible_inventory_group_natkey"),
    )


class AnsibleInventoryHost(Base):
    __tablename__ = "ansible_inventory_hosts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ansible_inventory_groups.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    vars: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    group: Mapped["AnsibleInventoryGroup"] = relationship(back_populates="hosts")
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_ansible_inventory_host_natkey"),
    )


class AnsiblePlaybookPlay(Base):
    __tablename__ = "ansible_playbook_plays"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    iac_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("iac_files.id"), index=True)
    # Play ``name`` is optional and may repeat within a file, so the natural key
    # uses the play's ordinal position rather than its name.
    play_index: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    hosts: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    iac_file: Mapped["IacFile"] = relationship(back_populates="plays")
    __table_args__ = (
        UniqueConstraint("iac_file_id", "play_index", name="uq_ansible_playbook_play_natkey"),
    )


class CiSchedule(Base):
    """A GitLab CI pipeline schedule, linked to the IaC file resource that owns it."""

    __tablename__ = "ci_schedules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # resource_id points to the IaC file Resource row (the .gitlab-ci.yml / schedule source).
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # branch or tag the schedule runs on
    ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # cron expression, e.g. "0 4 * * *"
    cron: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    active: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("project_id", "schedule_id", name="uq_ci_schedules_project_schedule"),
    )


__all__ = [
    "GitlabProject",
    "IacFile",
    "CiPipelineRun",
    "ComposeService",
    "K8sManifestResource",
    "TerraformResource",
    "AnsibleInventoryGroup",
    "AnsibleInventoryHost",
    "AnsiblePlaybookPlay",
    "CiSchedule",
]
