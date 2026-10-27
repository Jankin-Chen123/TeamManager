"""Initial schema, frozen independently of runtime models."""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def pk():
    return sa.Column("id", sa.Integer, primary_key=True)


def text(name, length=None):
    return sa.Column(name, sa.String(length) if length else sa.Text, nullable=False)


def upgrade():
    op.create_table(
        "proxy_nodes",
        pk(),
        text("name", 120),
        text("scheme", 12),
        text("host", 255),
        sa.Column("port", sa.Integer, nullable=False),
        text("username_enc"),
        text("password_enc"),
        sa.Column("enabled", sa.Boolean, nullable=False),
        text("note"),
    )
    op.create_table(
        "workspaces",
        pk(),
        text("name", 120),
        sa.Column("workspace_id", sa.String(128), unique=True, nullable=False),
        text("key_enc"),
        sa.Column(
            "node_id", sa.Integer, sa.ForeignKey("proxy_nodes.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        text("note"),
        sa.Column("last_probe_at", sa.Float),
        text("last_status", 64),
    )
    op.create_table(
        "probe_runs",
        pk(),
        sa.Column(
            "workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        text("node_name", 120),
        sa.Column("created_at", sa.Float, nullable=False),
        text("status", 64),
        text("details_json"),
        sa.Column("elapsed_ms", sa.Integer, nullable=False),
    )
    op.create_table(
        "jobs",
        pk(),
        sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="SET NULL")),
        text("kind", 64),
        text("status", 32),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float),
    )
    op.create_table(
        "job_items",
        pk(),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        text("target", 320),
        text("action", 40),
        text("status", 32),
        text("result_json"),
    )
    op.create_table(
        "audit_events",
        pk(),
        sa.Column("created_at", sa.Float, nullable=False),
        text("workspace_name", 120),
        text("node_name", 120),
        text("action", 64),
        text("target", 320),
        text("status", 64),
    )
    op.create_table("admin_accounts", pk(), text("password_hash"))
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        text("csrf", 64),
        sa.Column("expires_at", sa.Float, nullable=False),
    )


def downgrade():
    for name in [
        "sessions",
        "admin_accounts",
        "audit_events",
        "job_items",
        "jobs",
        "probe_runs",
        "workspaces",
        "proxy_nodes",
    ]:
        op.drop_table(name)
