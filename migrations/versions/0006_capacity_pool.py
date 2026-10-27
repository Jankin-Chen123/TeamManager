"""Universal CDKs, verified seat counts and durable allocation reservations."""

from alembic import op
import sqlalchemy as sa
import hashlib
import time

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind, default in [
        ("seat_limit", sa.Integer, "0"),
        ("seat_status", sa.String(64), "not_probed"),
        ("member_count", sa.Integer, "0"),
        ("invite_count", sa.Integer, "0"),
        ("seat_emails_json", sa.Text, "[]"),
    ]:
        op.add_column("workspaces", sa.Column(name, kind, nullable=False, server_default=default))
    op.add_column("workspaces", sa.Column("seat_checked_at", sa.Float))
    with op.batch_alter_table("redemptions") as batch:
        batch.add_column(sa.Column("workspace_id", sa.Integer, nullable=True))
        batch.create_foreign_key(
            "fk_redemption_workspace", "workspaces", ["workspace_id"], ["id"], ondelete="SET NULL"
        )
    op.execute(
        "UPDATE redemptions SET workspace_id = (SELECT workspace_id FROM jobs WHERE jobs.id = redemptions.job_id)"
    )
    # Redeemed records keep their actual destination; unredeemed codes become universal.
    op.execute(
        "UPDATE redemption_codes SET workspace_id = NULL WHERE id NOT IN (SELECT cdk_id FROM redemptions)"
    )
    op.create_table(
        "seat_reservations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("job_item_id", sa.Integer, sa.ForeignKey("job_items.id", ondelete="SET NULL"), unique=True),
        sa.Column("email_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_seat_reservations_workspace_id", "seat_reservations", ["workspace_id"])
    op.create_index("ix_seat_reservations_email_hash", "seat_reservations", ["email_hash"])
    connection = op.get_bind()
    # Preserve outstanding writes during upgrade, including successful writes whose
    # upstream visibility has not yet been verified. A snapshot reconciles them.
    rows = (
        connection.execute(
            sa.text(
                "SELECT i.id, i.target, i.status, j.workspace_id FROM job_items i "
                "JOIN jobs j ON j.id = i.job_id WHERE i.action = 'invite' "
                "AND i.status IN ('queued', 'running', 'uncertain', 'success') AND j.workspace_id IS NOT NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        connection.execute(
            sa.text(
                "INSERT INTO seat_reservations (workspace_id, job_item_id, email_hash, state, created_at) "
                "VALUES (:wid, :iid, :hashed, :state, :created)"
            ),
            {
                "wid": row["workspace_id"],
                "iid": row["id"],
                "hashed": hashlib.sha256(row["target"].lower().encode()).hexdigest(),
                "state": {"success": "submitted", "uncertain": "uncertain"}.get(row["status"], "reserved"),
                "created": time.time(),
            },
        )


def downgrade():
    raise RuntimeError("请使用升级前备份恢复数据库与匹配的加密主密钥")
