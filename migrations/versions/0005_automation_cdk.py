"""Persist node health, session renewal and one-use CDK redemptions."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind, default in [
        ("health_status", sa.String(24), "unknown"),
        ("health_error", sa.String(64), ""),
        ("failure_count", sa.Integer, "0"),
        ("blocked_until", sa.Float, "0"),
    ]:
        op.add_column("proxy_nodes", sa.Column(name, kind, nullable=False, server_default=default))
    op.add_column("proxy_nodes", sa.Column("health_checked_at", sa.Float, nullable=True))
    for name, kind, default in [
        ("auto_refresh", sa.Boolean, "1"),
        ("refresh_status", sa.String(64), "not_refreshed"),
        ("refresh_next_at", sa.Float, "0"),
        ("refresh_failures", sa.Integer, "0"),
    ]:
        op.add_column("workspaces", sa.Column(name, kind, nullable=False, server_default=default))
    op.add_column("workspaces", sa.Column("refresh_checked_at", sa.Float, nullable=True))
    op.create_table(
        "redemption_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="SET NULL")),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("expires_at", sa.Float),
        sa.Column("bound_email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "redemptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cdk_id", sa.Integer, sa.ForeignKey("redemption_codes.id"), nullable=False, unique=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_enc", sa.Text, nullable=False),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade():
    raise RuntimeError("请使用升级前备份恢复数据库与匹配的加密主密钥")
