"""Octopus Deploy relational model (20 classes).

Bucket: OctopusProjectGroup, OctopusLifecycle, OctopusProject,
        OctopusEnvironment, OctopusMachine, OctopusChannel, OctopusRelease,
        OctopusDeployment, OctopusDeploymentStep, OctopusVariable,
        OctopusLibraryVariableSet, OctopusActionTemplate, OctopusMachineRole,
        OctopusFeed, OctopusInterruption, OctopusTask, OctopusAccount,
        OctopusTeam, OctopusUser, OctopusEvent.

Design notes (preserved from original models.py):
- The generic ``Resource`` row stays the canonical handle; the detail tables for
  projects, environments, and machines hang off it via a unique ``resource_id``
  FK (the LinuxHost pattern).
- Reference tables that have no natural Resource (project groups, lifecycles,
  channels) stand alone and are linked by the Octopus string Id.
- ``octopus_id`` is the upstream Octopus identifier (e.g. "Projects-123") and
  is unique + the natural upsert key.
- ``octopus_variables`` and ``octopus_accounts`` deliberately carry NO
  value/secret column: names and metadata only.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

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
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONB, _uuid


class OctopusProjectGroup(Base):
    __tablename__ = "octopus_project_groups"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retention_policy_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class OctopusLifecycle(Base):
    __tablename__ = "octopus_lifecycles"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phases: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class OctopusProject(Base):
    __tablename__ = "octopus_projects"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    slug: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    project_group_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    project_group_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    lifecycle_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lifecycle_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    deployment_process_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # TRK-104: library variable set octopus_ids this project includes; drives the
    # KG project DEPENDS_ON library_variable_set edge.
    included_library_variable_set_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class OctopusEnvironment(Base):
    __tablename__ = "octopus_environments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class OctopusMachine(Base):
    __tablename__ = "octopus_machines"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), unique=True)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(64))
    status_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    communication_style: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tentacle_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    roles: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    environment_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    environment_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class OctopusChannel(Base):
    __tablename__ = "octopus_channels"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[str] = mapped_column(String(64))
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class OctopusRelease(Base):
    __tablename__ = "octopus_releases"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[str] = mapped_column(String(64))
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    version: Mapped[str] = mapped_column(String(128))
    channel_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    channel_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    assembled: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    release_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class OctopusDeployment(Base):
    __tablename__ = "octopus_deployments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[str] = mapped_column(String(64))
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    release_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    release_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    environment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    environment_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    finished_successfully: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)


# --- Octopus deep capture (MR9) ---
#
# The MR2 octopus_* tables cover projects/environments/machines/deployments/
# releases/channels/lifecycles/project-groups. These add the deeper objects:
# deployment-process steps, variable METADATA (never values), library variable
# sets, action templates, machine roles, feeds, interruptions, tasks, accounts
# (never secret material), teams, users, and events. Most key by the upstream
# Octopus string id (no FK) and are queried directly, matching the existing
# octopus pattern. The two zone-sensitive tables — octopus_variables and
# octopus_accounts — deliberately carry NO value/secret column: names and
# metadata only.


class OctopusDeploymentStep(Base):
    __tablename__ = "octopus_deployment_steps"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    project_octopus_id: Mapped[str] = mapped_column(String(64), index=True)
    project_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    deployment_process_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    step_order: Mapped[int] = mapped_column(Integer)
    step_name: Mapped[str] = mapped_column(String(256))
    action_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    action_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_roles: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    channels: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    environments: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    feed_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    package_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    condition: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "project_octopus_id",
            "step_order",
            "action_name",
            name="uq_octopus_deployment_step_natkey",
        ),
    )


class OctopusVariable(Base):
    # METADATA ONLY: no value column, ever. Captures the existence and scoping of
    # a variable so we can reason about deployment configuration without ingesting
    # any secret/sensitive payload.
    __tablename__ = "octopus_variables"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_octopus_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    # ``ordinal`` disambiguates the same name repeated across different scopes
    # (the scope itself is captured in the JSONB ``scope`` column); together
    # (owner_octopus_id, name, ordinal) is the upsert natural key.
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("owner_octopus_id", "name", "ordinal", name="uq_octopus_variable_natkey"),
    )


class OctopusLibraryVariableSet(Base):
    __tablename__ = "octopus_library_variable_sets"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    variable_set_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class OctopusActionTemplate(Base):
    __tablename__ = "octopus_action_templates"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    action_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class OctopusMachineRole(Base):
    __tablename__ = "octopus_machine_roles"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    role_name: Mapped[str] = mapped_column(String(256), unique=True)


class OctopusFeed(Base):
    __tablename__ = "octopus_feeds"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    feed_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    feed_uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class OctopusInterruption(Base):
    __tablename__ = "octopus_interruptions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    # task_id is written on upsert but never used as a query predicate; its index
    # (ix_octopus_interruptions_task_id) was confirmed dead and dropped in MR-F.
    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    responsible_team_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    related_document_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    created: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class OctopusTask(Base):
    __tablename__ = "octopus_tasks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    finished_successfully: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    has_warnings_or_errors: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    server_node: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class OctopusAccount(Base):
    # METADATA ONLY: no secret/key/token material is ever stored. Captures the
    # name/type/scope of an Octopus account for inventory and audit purposes.
    __tablename__ = "octopus_accounts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    account_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    environment_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class OctopusTeam(Base):
    __tablename__ = "octopus_teams"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    member_user_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    project_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    environment_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    user_role_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)


class OctopusUser(Base):
    __tablename__ = "octopus_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_service: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)


class OctopusEvent(Base):
    __tablename__ = "octopus_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    octopus_id: Mapped[str] = mapped_column(String(64), unique=True)
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # occurred is captured for display but no query filters/sorts on it; its
    # index (ix_octopus_events_occurred) was confirmed dead and dropped in MR-F.
    occurred: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_document_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)


# octopus deep capture (MR9): natural-key / lookup indexes back the upserts
Index(
    "ix_octopus_deployment_steps_natkey",
    OctopusDeploymentStep.project_octopus_id,
    OctopusDeploymentStep.step_order,
)
Index(
    "ix_octopus_variables_natkey",
    OctopusVariable.owner_octopus_id,
    OctopusVariable.name,
)

# octopus_deployments: the /octopus/deployments endpoint sorts by completed_at
# (DESC, nulls last) and optionally filters by project_id / environment_id and a
# completed_at range. These back that pagination pushdown (S-10/11/12, MR-F).
Index("ix_octopus_deployments_completed_at", OctopusDeployment.completed_at)
Index("ix_octopus_deployments_project_id", OctopusDeployment.project_id)
Index("ix_octopus_deployments_environment_id", OctopusDeployment.environment_id)


__all__ = [
    "OctopusProjectGroup",
    "OctopusLifecycle",
    "OctopusProject",
    "OctopusEnvironment",
    "OctopusMachine",
    "OctopusChannel",
    "OctopusRelease",
    "OctopusDeployment",
    "OctopusDeploymentStep",
    "OctopusVariable",
    "OctopusLibraryVariableSet",
    "OctopusActionTemplate",
    "OctopusMachineRole",
    "OctopusFeed",
    "OctopusInterruption",
    "OctopusTask",
    "OctopusAccount",
    "OctopusTeam",
    "OctopusUser",
    "OctopusEvent",
]
