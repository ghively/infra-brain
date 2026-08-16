"""IaC / CI-CD relational model.

Revision ID: 0006_iac_cicd_relational
Revises: 0005_octopus_rich_model
Create Date: 2026-06-23

Adds the pragmatic-relational model for GitLab projects, parsed IaC files, and
CI pipeline runs, plus the per-file detail children:

  * gitlab_projects        (keyed on gitlab_project_id, unique)
  * iac_files              (natural key gitlab_project_id+path+ref+file_type)
  * ci_pipeline_runs       (natural key gitlab_project_id+pipeline_id)
  * compose_services       (child of iac_files)
  * k8s_manifest_resources (child of iac_files)
  * terraform_resources    (child of iac_files)
  * ansible_inventory_groups / ansible_inventory_hosts (child chain of iac_files)
  * ansible_playbook_plays (child of iac_files)

Each primary table carries a JSONB ``details`` column for nested extras.

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install these tables already exist when this migration
runs. We therefore guard every create with the inspector (mirroring 0005).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0006_iac_cicd_relational"
down_revision = "0005_octopus_rich_model"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "gitlab_projects" not in tables:
        op.create_table(
            "gitlab_projects",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("gitlab_project_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("path_with_namespace", sa.String(512), nullable=True),
            sa.Column("default_branch", sa.String(128), nullable=True),
            sa.Column("visibility", sa.String(32), nullable=True),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("group_id", sa.Integer(), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_pipeline_id", sa.Integer(), nullable=True),
            sa.Column("last_pipeline_status", sa.String(32), nullable=True),
            sa.Column("last_pipeline_ref", sa.String(256), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("gitlab_project_id", name="uq_gitlab_project_id"),
        )
        op.create_index(
            "ix_gitlab_projects_gitlab_project_id", "gitlab_projects", ["gitlab_project_id"]
        )

    if "iac_files" not in tables:
        op.create_table(
            "iac_files",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("gitlab_project_id", sa.Integer(), nullable=False),
            sa.Column("project_name", sa.String(256), nullable=True),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("file_type", sa.String(32), nullable=False),
            sa.Column("ref", sa.String(128), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint(
                "gitlab_project_id", "path", "ref", "file_type", name="uq_iac_file_natkey"
            ),
        )
        op.create_index("ix_iac_files_gitlab_project_id", "iac_files", ["gitlab_project_id"])

    if "ci_pipeline_runs" not in tables:
        op.create_table(
            "ci_pipeline_runs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("gitlab_project_id", sa.Integer(), nullable=False),
            sa.Column("project_name", sa.String(256), nullable=True),
            sa.Column("pipeline_id", sa.Integer(), nullable=False),
            sa.Column("ref", sa.String(256), nullable=True),
            sa.Column("sha", sa.String(64), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("source", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration", sa.Integer(), nullable=True),
            sa.Column("web_url", sa.String(512), nullable=True),
            sa.UniqueConstraint(
                "gitlab_project_id", "pipeline_id", name="uq_ci_pipeline_run_natkey"
            ),
        )
        op.create_index(
            "ix_ci_pipeline_runs_gitlab_project_id", "ci_pipeline_runs", ["gitlab_project_id"]
        )
        op.create_index("ix_ci_pipeline_runs_pipeline_id", "ci_pipeline_runs", ["pipeline_id"])

    if "compose_services" not in tables:
        op.create_table(
            "compose_services",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("iac_file_id", sa.Uuid(), sa.ForeignKey("iac_files.id"), nullable=False),
            sa.Column("service_name", sa.String(256), nullable=False),
            sa.Column("image", sa.String(512), nullable=True),
            sa.Column("ports", _JSONB, nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("iac_file_id", "service_name", name="uq_compose_service_natkey"),
        )
        op.create_index("ix_compose_services_iac_file_id", "compose_services", ["iac_file_id"])

    if "k8s_manifest_resources" not in tables:
        op.create_table(
            "k8s_manifest_resources",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("iac_file_id", sa.Uuid(), sa.ForeignKey("iac_files.id"), nullable=False),
            sa.Column("kind", sa.String(128), nullable=False),
            sa.Column("api_version", sa.String(64), nullable=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("namespace", sa.String(128), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint(
                "iac_file_id",
                "kind",
                "name",
                "namespace",
                name="uq_k8s_manifest_resource_natkey",
            ),
        )
        op.create_index(
            "ix_k8s_manifest_resources_iac_file_id",
            "k8s_manifest_resources",
            ["iac_file_id"],
        )

    if "terraform_resources" not in tables:
        op.create_table(
            "terraform_resources",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("iac_file_id", sa.Uuid(), sa.ForeignKey("iac_files.id"), nullable=False),
            sa.Column("resource_type", sa.String(128), nullable=False),
            sa.Column("resource_name", sa.String(256), nullable=False),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint(
                "iac_file_id",
                "resource_type",
                "resource_name",
                name="uq_terraform_resource_natkey",
            ),
        )
        op.create_index(
            "ix_terraform_resources_iac_file_id", "terraform_resources", ["iac_file_id"]
        )

    if "ansible_inventory_groups" not in tables:
        op.create_table(
            "ansible_inventory_groups",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("iac_file_id", sa.Uuid(), sa.ForeignKey("iac_files.id"), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.UniqueConstraint("iac_file_id", "name", name="uq_ansible_inventory_group_natkey"),
        )
        op.create_index(
            "ix_ansible_inventory_groups_iac_file_id",
            "ansible_inventory_groups",
            ["iac_file_id"],
        )

    if "ansible_inventory_hosts" not in tables:
        op.create_table(
            "ansible_inventory_hosts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "group_id",
                sa.Uuid(),
                sa.ForeignKey("ansible_inventory_groups.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("vars", _JSONB, nullable=True),
            sa.UniqueConstraint("group_id", "name", name="uq_ansible_inventory_host_natkey"),
        )
        op.create_index(
            "ix_ansible_inventory_hosts_group_id",
            "ansible_inventory_hosts",
            ["group_id"],
        )

    if "ansible_playbook_plays" not in tables:
        op.create_table(
            "ansible_playbook_plays",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("iac_file_id", sa.Uuid(), sa.ForeignKey("iac_files.id"), nullable=False),
            sa.Column("play_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(256), nullable=True),
            sa.Column("hosts", _JSONB, nullable=True),
            sa.UniqueConstraint(
                "iac_file_id", "play_index", name="uq_ansible_playbook_play_natkey"
            ),
        )
        op.create_index(
            "ix_ansible_playbook_plays_iac_file_id",
            "ansible_playbook_plays",
            ["iac_file_id"],
        )


def downgrade() -> None:
    # Drop children before parents (FK-safe order).
    for tbl in (
        "ansible_playbook_plays",
        "ansible_inventory_hosts",
        "ansible_inventory_groups",
        "terraform_resources",
        "k8s_manifest_resources",
        "compose_services",
        "ci_pipeline_runs",
        "iac_files",
        "gitlab_projects",
    ):
        op.drop_table(tbl)
