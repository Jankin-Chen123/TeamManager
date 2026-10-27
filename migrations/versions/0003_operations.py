"""Persistent operating settings and traceable node, task and audit results."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_tests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("node_id", sa.Integer, sa.ForeignKey("proxy_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("elapsed_ms", sa.Integer, nullable=False),
    )
    op.create_index("ix_node_tests_node", "node_tests", ["node_id", "id"])
    with op.batch_alter_table("proxy_nodes") as batch:
        batch.add_column(sa.Column("last_test_at", sa.Float, nullable=True))
        batch.add_column(
            sa.Column("last_test_status", sa.String(64), nullable=False, server_default="not_probed")
        )
        batch.add_column(sa.Column("last_test_ip", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("last_test_ms", sa.Integer, nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("workspace_name", sa.String(120), nullable=False, server_default=""))
        batch.add_column(sa.Column("started_at", sa.Float, nullable=True))
    # ALTER ADD preserves referencing tables while applying a live SQLite migration.
    op.execute(
        "ALTER TABLE audit_events ADD COLUMN workspace_id INTEGER REFERENCES workspaces (id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE audit_events ADD COLUMN job_id INTEGER REFERENCES jobs (id) ON DELETE SET NULL")
    op.add_column("audit_events", sa.Column("error_code", sa.String(64), nullable=False, server_default=""))
    op.add_column("audit_events", sa.Column("request_id", sa.String(128), nullable=False, server_default=""))
    op.create_index("ix_audit_workspace", "audit_events", ["workspace_id", "id"])
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_timeout", sa.Float, nullable=False),
        sa.Column("concurrency", sa.Integer, nullable=False),
        sa.Column("log_retention_days", sa.Integer, nullable=False),
    )
    op.execute(
        "UPDATE jobs SET workspace_name = COALESCE((SELECT name FROM workspaces WHERE workspaces.id = jobs.workspace_id), '')"
    )


def downgrade():
    # Rebuilding referenced parent tables can cascade-delete child records in SQLite.
    # Roll back this release using the verified pre-upgrade database backup instead.
    raise RuntimeError("请使用升级前数据库备份回退 0003，禁止破坏引用关系的原地降级")
