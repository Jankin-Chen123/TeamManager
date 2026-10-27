"""Add encrypted website-session credentials without changing existing access mode."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspaces", sa.Column("credential_type", sa.String(24), nullable=False, server_default="admin_api")
    )
    op.add_column("workspaces", sa.Column("session_token_enc", sa.Text, nullable=False, server_default=""))
    op.add_column("workspaces", sa.Column("token_expires_at", sa.Float, nullable=True))


def downgrade():
    raise RuntimeError("请使用升级前备份恢复数据库与匹配的加密主密钥")
