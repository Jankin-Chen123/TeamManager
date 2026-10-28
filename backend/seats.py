"""Seat snapshots and reservations; no allocation against unknown or stale counts."""

import asyncio
import json

from fastapi import HTTPException
from sqlalchemy import select

from backend.models import Workspace, Node, SeatReservation, JobItem, Redemption, now
from backend.security import token_hash
from backend.upstream import UpstreamError, clean_page

# Idle display can lag; real allocations retain the five-minute snapshot limit
# and every invitation still forces an upstream check immediately before writing.
SNAPSHOT_TTL = 300
IDLE_SNAPSHOT_TTL = 1800
HELD_STATES = {"reserved", "submitted", "uncertain"}
BINDING_FIELDS = ("workspace_id", "node_id", "key_enc", "credential_type")


class Seats:
    def __init__(self, services):
        self.services, self.db = services, services.db
        self.allocation_lock = asyncio.Lock()
        self.pending = set()
        self.retry_at = {}

    def fresh(self, workspace, max_age=SNAPSHOT_TTL):
        return (
            workspace.seat_status == "success"
            and workspace.seat_checked_at is not None
            and now() - workspace.seat_checked_at <= max_age
        )

    def route_ready(self, node):
        return (
            node
            and node.enabled
            and node.blocked_until <= now()
            and node.health_status not in {"unhealthy", "suspect"}
        )

    def info(self, db, workspace):
        occupied_emails = set(json.loads(workspace.seat_emails_json))
        held = set(
            db.scalars(
                select(SeatReservation.email_hash).where(
                    SeatReservation.workspace_id == workspace.id, SeatReservation.state.in_(HELD_STATES)
                )
            )
        )
        reserved = len(held - occupied_emails)
        fresh = self.fresh(workspace, IDLE_SNAPSHOT_TTL)
        node = db.get(Node, workspace.node_id)
        eligible = bool(workspace.enabled and workspace.seat_limit > 0 and fresh and self.route_ready(node))
        available = max(0, workspace.seat_limit - workspace.member_count - workspace.invite_count - reserved)
        return {
            "total": workspace.seat_limit,
            "members": workspace.member_count,
            "invites": workspace.invite_count,
            "reserved": reserved,
            "available": available if eligible else 0,
            "eligible": eligible,
            "fresh": fresh,
            "checked_at": workspace.seat_checked_at,
            "status": workspace.seat_status,
        }

    def summary(self):
        with self.db() as db:
            rows = list(db.scalars(select(Workspace).order_by(Workspace.name, Workspace.id)))
            values = [self.info(db, w) for w in rows]
        return {
            "available": sum(v["available"] for v in values),
            "ready_workspaces": sum(v["eligible"] for v in values),
            "checked_at": min((v["checked_at"] for v in values if v["eligible"]), default=None),
            "snapshot_max_age_seconds": IDLE_SNAPSHOT_TTL,
        }

    async def collect(self, workspace, node, resource, trace=None):
        result, cursors, after = {}, set(), None
        signatures = set()
        for _ in range(100):
            params = {"limit": 100}
            if after:
                params["after"] = after
            raw = await self.services.gateway.workspace(workspace, node, "GET", resource, params=params)
            if trace is not None:
                trace["request_id"] = getattr(raw, "request_id", "")
            if not isinstance(raw.get("data"), list) or any(not isinstance(r, dict) for r in raw["data"]):
                raise UpstreamError("invalid_response")
            page = clean_page(raw, resource)
            signature = tuple((r.get("id"), r.get("email")) for r in page["data"])
            if signature and signature in signatures and (page["has_more"] or len(page["data"]) >= 100):
                raise UpstreamError("invalid_response")
            signatures.add(signature)
            for row in page["data"]:
                email = row.get("email")
                identity = row.get("id")
                key = (
                    "email:" + email.strip().lower()
                    if isinstance(email, str) and email.strip()
                    else "id:" + str(identity or "")
                )
                if key == "id:":
                    raise UpstreamError("invalid_response")
                result[key] = row
            if not page["has_more"]:
                return result
            after = page["last_id"]
            if after in cursors:
                raise UpstreamError("invalid_response")
            cursors.add(after)
        raise UpstreamError("lookup_limit")

    async def refresh(self, workspace_id, force=True):
        async with self.services.lock(workspace_id), self.services.semaphore:
            return await self.refresh_locked(workspace_id, force)

    async def refresh_locked(self, workspace_id, force=True, diagnostics=None):
        workspace, node = self.services.pair(workspace_id)
        if not force and self.fresh(workspace):
            return
        if not force and self.retry_at.get(workspace_id, 0) > now():
            return
        try:
            pages, failure = {}, None
            for resource in ("users", "invites"):
                try:
                    trace = {}
                    pages[resource] = await self.collect(workspace, node, resource, trace)
                    if diagnostics is not None:
                        diagnostics[resource] = {"code": "success", **trace}
                except UpstreamError as exc:
                    if diagnostics is None:
                        raise
                    diagnostics[resource] = exc.public()
                    failure = failure or exc
            if failure:
                raise failure
            members, invites = pages["users"], pages["invites"]
            invites = {
                key: row
                for key, row in invites.items()
                if row.get("status", "pending") not in {"accepted", "expired", "revoked", "cancelled"}
                and key not in members
            }
            hashes = {
                token_hash(key[6:]) for key in members.keys() | invites.keys() if key.startswith("email:")
            }
            with self.db.begin() as db:
                live = db.get(Workspace, workspace_id)
                if not live or any(getattr(live, k) != getattr(workspace, k) for k in BINDING_FIELDS):
                    raise HTTPException(409, "席位刷新期间空间配置已变更，请重新操作")
                live.member_count, live.invite_count = len(members), len(invites)
                live.seat_emails_json = json.dumps(sorted(hashes))
                live.seat_checked_at, live.seat_status = now(), "success"
                for reservation in db.scalars(
                    select(SeatReservation).where(
                        SeatReservation.workspace_id == workspace_id,
                        SeatReservation.state.in_(HELD_STATES),
                    )
                ):
                    if reservation.email_hash in hashes:
                        reservation.state = "reflected"
                db.flush()
                result = self.info(db, live)
            self.services.audit("refresh_seats", "success", workspace, node)
            self.retry_at.pop(workspace_id, None)
            return result
        except UpstreamError as exc:
            self.retry_at[workspace_id] = now() + SNAPSHOT_TTL
            with self.db.begin() as db:
                live = db.get(Workspace, workspace_id)
                if live and all(getattr(live, k) == getattr(workspace, k) for k in BINDING_FIELDS):
                    live.seat_status = exc.code
            self.services.audit("refresh_seats", exc.code, workspace, node, error_code=exc.code)
            raise

    async def ensure_pool(self):
        with self.db() as db:
            candidates = [
                (w.id, w.node_id)
                for w in db.scalars(
                    select(Workspace)
                    .where(Workspace.enabled.is_(True), Workspace.seat_limit > 0)
                    .order_by(Workspace.name, Workspace.id)
                )
                if not self.fresh(w) and self.route_ready(db.get(Node, w.node_id))
            ]
        for wid, _ in candidates:
            try:
                await self.refresh(wid, force=False)
            except (UpstreamError, HTTPException):
                # An unverified space contributes zero, but other spaces may still be usable.
                continue

    def choose(self, db, email):
        hashed = token_hash(email.lower())
        for w in db.scalars(select(Workspace)):
            if w.enabled and self.fresh(w) and hashed in set(json.loads(w.seat_emails_json)):
                raise HTTPException(409, "该邮箱已有成员或待处理邀请，请联系管理员核查；兑换码尚未使用")
        if db.scalar(
            select(SeatReservation.id).where(
                SeatReservation.email_hash == hashed, SeatReservation.state.in_(HELD_STATES)
            )
        ):
            raise HTTPException(409, "该邮箱已有待处理申请；兑换码尚未使用")
        for workspace in db.scalars(select(Workspace).order_by(Workspace.name, Workspace.id)):
            if self.fresh(workspace) and self.info(db, workspace)["available"] > 0:
                return workspace
        raise HTTPException(503, "当前没有已确认的有效席位，请稍后重试；兑换码尚未使用")

    async def check_invite_locked(self, workspace_id, item_id, email):
        # Caller already owns the workspace lock and concurrency gate.
        with self.db() as db:
            workspace = db.get(Workspace, workspace_id)
            item = db.get(JobItem, item_id)
            public = db.scalar(select(Redemption.id).where(Redemption.job_id == item.job_id))
            managed = workspace.seat_limit > 0 or public is not None
        if not managed:
            return
        await self.refresh_locked(workspace_id)
        with self.db() as db:
            workspace = db.get(Workspace, workspace_id)
            hashes = set(json.loads(workspace.seat_emails_json))
            hashed = token_hash(email.lower())
            if hashed in hashes:
                raise UpstreamError("invite_exists")
            earlier = (
                set(
                    db.scalars(
                        select(SeatReservation.email_hash).where(
                            SeatReservation.workspace_id == workspace_id,
                            SeatReservation.state.in_(HELD_STATES),
                            (SeatReservation.job_item_id < item_id) | SeatReservation.job_item_id.is_(None),
                        )
                    )
                )
                - hashes
            )
            if (
                not self.fresh(workspace)
                or workspace.seat_limit <= 0
                or not self.route_ready(db.get(Node, workspace.node_id))
                or workspace.seat_limit - workspace.member_count - workspace.invite_count - len(earlier) < 1
            ):
                raise UpstreamError("seats_full")

    def reserve(self, db, workspace_id, item, email):
        db.add(
            SeatReservation(
                workspace_id=workspace_id, job_item_id=item.id, email_hash=token_hash(email.lower())
            )
        )

    def settle(self, job_id):
        with self.db.begin() as db:
            for reservation, item in db.execute(
                select(SeatReservation, JobItem)
                .join(JobItem, JobItem.id == SeatReservation.job_item_id)
                .where(JobItem.job_id == job_id)
            ):
                if reservation.state in {"released", "reflected"}:
                    continue
                if item.status in {"failed", "cancelled"}:
                    reservation.state = "released"
                elif reservation.state != "reflected":
                    reservation.state = {"success": "submitted", "uncertain": "uncertain"}.get(
                        item.status, "reserved"
                    )

    async def background(self, workspace_id):
        try:
            await self.refresh(workspace_id, force=False)
        except (UpstreamError, HTTPException):
            pass
        finally:
            self.pending.discard(workspace_id)

    def tick(self):
        with self.db() as db:
            ids = [
                w.id
                for w in db.scalars(
                    select(Workspace).where(Workspace.enabled.is_(True), Workspace.seat_limit > 0)
                )
                if w.id not in self.services.pending_probes
                and not self.services.lock(w.id).locked()
                and not self.fresh(w, IDLE_SNAPSHOT_TTL)
                and self.route_ready(db.get(Node, w.node_id))
                and self.retry_at.get(w.id, 0) <= now()
            ]
        for wid in ids:
            if wid not in self.pending:
                self.pending.add(wid)
                self.services.spawn(self.background(wid))
