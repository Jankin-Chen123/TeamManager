"""Indexes for workspace history, node references and batch job lookups."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_workspaces_node", "workspaces", ["node_id"])
    op.create_index("ix_probes_workspace_id", "probe_runs", ["workspace_id", "id"])
    op.create_index("ix_job_items_job_id", "job_items", ["job_id", "id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade():
    for name, table in [
        ("ix_jobs_status", "jobs"),
        ("ix_job_items_job_id", "job_items"),
        ("ix_probes_workspace_id", "probe_runs"),
        ("ix_workspaces_node", "workspaces"),
    ]:
        op.drop_index(name, table_name=table)
