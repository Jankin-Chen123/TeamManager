from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc).timestamp()


class Base(DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "proxy_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    scheme: Mapped[str] = mapped_column(String(12))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username_enc: Mapped[str] = mapped_column(Text, default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    last_test_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_test_status: Mapped[str] = mapped_column(String(64), default="not_probed")
    last_test_ip: Mapped[str] = mapped_column(String(64), default="")
    last_test_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_status: Mapped[str] = mapped_column(String(24), default="unknown")
    health_checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    health_error: Mapped[str] = mapped_column(String(64), default="")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[float] = mapped_column(Float, default=0)


class NodeTest(Base):
    __tablename__ = "node_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("proxy_nodes.id", ondelete="CASCADE"))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    status: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str] = mapped_column(String(64), default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    workspace_id: Mapped[str] = mapped_column(String(128), unique=True)
    key_enc: Mapped[str] = mapped_column(Text)
    credential_type: Mapped[str] = mapped_column(String(24), default="admin_api")
    session_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    auto_refresh: Mapped[bool] = mapped_column(Boolean, default=True)
    refresh_status: Mapped[str] = mapped_column(String(64), default="not_refreshed")
    refresh_checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    refresh_next_at: Mapped[float] = mapped_column(Float, default=0)
    refresh_failures: Mapped[int] = mapped_column(Integer, default=0)
    seat_limit: Mapped[int] = mapped_column(Integer, default=0)
    seat_checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    seat_status: Mapped[str] = mapped_column(String(64), default="not_probed")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    invite_count: Mapped[int] = mapped_column(Integer, default=0)
    seat_emails_json: Mapped[str] = mapped_column(Text, default="[]")
    node_id: Mapped[int] = mapped_column(ForeignKey("proxy_nodes.id", ondelete="RESTRICT"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    last_probe_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_status: Mapped[str] = mapped_column(String(64), default="not_probed")


class Probe(Base):
    __tablename__ = "probe_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    node_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    status: Mapped[str] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text)
    elapsed_ms: Mapped[int] = mapped_column(Integer)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    workspace_name: Mapped[str] = mapped_column(String(120), default="")
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class JobItem(Base):
    __tablename__ = "job_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    target: Mapped[str] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    result_json: Mapped[str] = mapped_column(Text, default="{}")


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    workspace_name: Mapped[str] = mapped_column(String(120), default="")
    node_name: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(320), default="")
    status: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    error_code: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(128), default="")


class SystemSettings(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_timeout: Mapped[float] = mapped_column(Float)
    concurrency: Mapped[int] = mapped_column(Integer)
    log_retention_days: Mapped[int] = mapped_column(Integer, default=90)


class Account(Base):
    __tablename__ = "admin_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)


class CDK(Base):
    __tablename__ = "redemption_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    bound_email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Redemption(Base):
    __tablename__ = "redemptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    cdk_id: Mapped[int] = mapped_column(ForeignKey("redemption_codes.id"), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    receipt_hash: Mapped[str] = mapped_column(String(64), unique=True)
    receipt_enc: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))


class SeatReservation(Base):
    __tablename__ = "seat_reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    job_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_items.id", ondelete="SET NULL"), unique=True
    )
    email_hash: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(24), default="reserved")
    created_at: Mapped[float] = mapped_column(Float, default=now)
