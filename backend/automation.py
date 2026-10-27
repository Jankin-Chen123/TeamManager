"""Background maintenance shares the same bound proxies, locks and concurrency gate."""

import asyncio
import json
import math

from fastapi import HTTPException
from sqlalchemy import select

from backend.models import Node, Workspace, now
from backend.upstream import UpstreamError
from backend.web_session import WebSessionGateway, import_credentials

ROUTE_FAILURES = {"proxy_connection", "proxy_auth"}
AMBIGUOUS_FAILURES = {"timeout", "network_error", "tls_error"}
REACHED_TARGET = {
    "success",
    "invalid_key",
    "session_expired",
    "permission_denied",
    "unavailable",
    "rate_limited",
    "upstream_error",
    "request_rejected",
    "web_verification",
    "redirect_blocked",
    "invalid_response",
}
ROUTE_FIELDS = ("scheme", "host", "port", "username_enc", "password_enc", "enabled")


class Automation:
    def __init__(self, services):
        self.services = services
        self.db = services.db
        self.vault = services.gateway.vault
        self.pending_refresh = set()
        self.pending_nodes = set()

    def check_node(self, node):
        with self.db() as db:
            live = db.get(Node, node.id)
            if not live or not live.enabled:
                raise UpstreamError("proxy_disabled")
            if live.blocked_until > now():
                raise UpstreamError("node_cooling", retry_after=math.ceil(live.blocked_until - now()))

    def report_node(self, node, code):
        if code not in ROUTE_FAILURES | AMBIGUOUS_FAILURES | REACHED_TARGET:
            return
        with self.db.begin() as db:
            live = db.get(Node, node.id)
            if not live or any(getattr(live, k) != getattr(node, k) for k in ROUTE_FIELDS):
                return
            live.health_checked_at = now()
            if code in REACHED_TARGET:
                live.health_status, live.health_error = "healthy", ""
                live.failure_count, live.blocked_until = 0, 0
            else:
                live.health_status = "unhealthy" if code in ROUTE_FAILURES else "suspect"
                live.health_error = code
                live.failure_count = min(live.failure_count + 1, 20)
                live.blocked_until = now() + min(30 * 2 ** (live.failure_count - 1), 600)

    def observe(self, workspace, node, code):
        self.report_node(node, code)
        if code == "session_expired":
            with self.db.begin() as db:
                live = db.get(Workspace, workspace.id)
                if live and live.key_enc == workspace.key_enc:
                    live.refresh_status = "session_expired"
                    # At most once per five minutes for a rejected token.
                    live.refresh_next_at = max(now(), (live.refresh_checked_at or 0) + 300)

    def due(self, workspace):
        return (
            workspace.auto_refresh
            and workspace.enabled
            and workspace.credential_type == "web_session"
            and workspace.refresh_next_at <= now()
            and (
                not workspace.token_expires_at
                or workspace.token_expires_at <= now() + 3600
                or workspace.refresh_status == "session_expired"
            )
        )

    async def prepare(self, workspace, node):
        if not workspace.enabled:
            raise UpstreamError("workspace_disabled")
        self.check_node(node)
        if self.due(workspace):
            try:
                await self.refresh(workspace, node)
            except UpstreamError as exc:
                # Never replay a write; renewal happens before the original operation.
                if exc.code in ROUTE_FAILURES | AMBIGUOUS_FAILURES | {"node_cooling", "proxy_disabled"}:
                    raise
                if workspace.token_expires_at and workspace.token_expires_at <= now():
                    raise

    async def refresh(self, workspace, node):
        try:
            self.check_node(node)
            if workspace.credential_type != "web_session" or not workspace.session_token_enc:
                raise UpstreamError("session_cookie_missing")
            raw = await WebSessionGateway(self.vault, self.services.gateway.timeout).request(
                node,
                "GET",
                "/api/auth/session",
                cookie=self.vault.decrypt(workspace.session_token_enc),
                params={
                    "exchange_workspace_token": "true",
                    "workspace_id": workspace.workspace_id,
                    "reason": "setCurrentAccount",
                },
            )
            self.report_node(node, "success")
            try:
                token, cookie, expiry = import_credentials("", json.dumps(raw), "", workspace.workspace_id)
            except ValueError:
                raise UpstreamError("session_expired") from None
            if not token:
                raise UpstreamError("session_expired")
            with self.db.begin() as db:
                live = db.get(Workspace, workspace.id)
                if not live or any(
                    getattr(live, k) != getattr(workspace, k)
                    for k in (
                        "key_enc",
                        "session_token_enc",
                        "workspace_id",
                        "node_id",
                        "credential_type",
                        "auto_refresh",
                    )
                ):
                    raise HTTPException(409, "刷新期间空间配置已变更，请重新操作")
                live.key_enc, live.token_expires_at = self.vault.encrypt(token), expiry
                if cookie:
                    live.session_token_enc = self.vault.encrypt(cookie)
                live.refresh_checked_at, live.refresh_status, live.refresh_failures = now(), "success", 0
                live.refresh_next_at = max(now() + 60, expiry - 3600) if expiry else now() + 21600
                # The waiting original request must use the newly encrypted token.
                for key in (
                    "key_enc",
                    "token_expires_at",
                    "session_token_enc",
                    "refresh_next_at",
                    "refresh_status",
                ):
                    setattr(workspace, key, getattr(live, key))
            self.services.audit("refresh_session", "success", workspace, node, request_id=raw.request_id)
            return {"ok": True, "token_expires_at": expiry}
        except UpstreamError as exc:
            self.report_node(node, exc.code)
            with self.db.begin() as db:
                live = db.get(Workspace, workspace.id)
                if live and live.key_enc == workspace.key_enc and live.node_id == workspace.node_id:
                    live.refresh_status, live.refresh_checked_at = exc.code, now()
                    live.refresh_failures = min(live.refresh_failures + 1, 20)
                    delay = (
                        21600
                        if exc.code
                        in {
                            "session_cookie_missing",
                            "session_expired",
                            "web_verification",
                            "permission_denied",
                        }
                        else min(60 * 2 ** (live.refresh_failures - 1), 3600)
                    )
                    live.refresh_next_at = now() + max(delay, exc.retry_after or 0)
            self.services.audit(
                "refresh_session", exc.code, workspace, node, error_code=exc.code, request_id=exc.request_id
            )
            raise

    async def refresh_one(self, workspace_id):
        try:
            async with self.services.lock(workspace_id), self.services.semaphore:
                workspace, node = self.services.pair(workspace_id)
                if self.due(workspace):
                    async with self.services.gateway.node_locks.setdefault(node.id, asyncio.Lock()):
                        await self.refresh(workspace, node)
        except (UpstreamError, HTTPException):
            pass
        finally:
            self.pending_refresh.discard(workspace_id)

    async def recover_node(self, node_id):
        try:
            async with self.services.semaphore:
                with self.db() as db:
                    node = db.get(Node, node_id)
                if node and node.enabled:
                    async with self.services.gateway.node_locks.setdefault(node.id, asyncio.Lock()):
                        await self.services.test_node(node)
                    self.services.audit("recover_node", "success", node=node)
        except UpstreamError:
            pass
        finally:
            self.pending_nodes.discard(node_id)

    def tick(self):
        with self.db() as db:
            workspaces = list(db.scalars(select(Workspace).where(Workspace.enabled.is_(True))))
            nodes = list(db.scalars(select(Node).where(Node.enabled.is_(True))))
        for workspace in workspaces:
            if self.due(workspace) and workspace.id not in self.pending_refresh:
                self.pending_refresh.add(workspace.id)
                self.services.spawn(self.refresh_one(workspace.id))
        for node in nodes:
            if (
                node.health_status in {"unhealthy", "suspect"}
                and node.blocked_until <= now()
                and node.id not in self.pending_nodes
            ):
                self.pending_nodes.add(node.id)
                self.services.spawn(self.recover_node(node.id))
