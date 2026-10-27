import asyncio
import json
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from cryptography.fernet import InvalidToken

from backend.config import Settings
from backend.db import database
from backend.models import (
    Account,
    Audit,
    Job,
    JobItem,
    Node,
    NodeTest,
    Probe,
    Session,
    SystemSettings,
    Workspace,
    SeatReservation,
    now,
)
from backend.schemas import (
    ConfirmInput,
    InviteInput,
    LoginInput,
    NodeInput,
    PasswordInput,
    SettingsInput,
    WorkspaceInput,
)
from backend.security import Vault, hash_password, token_hash, verify_password
from backend.services import Services
from backend.upstream import Gateway, UpstreamError
from backend.web_session import import_credentials


def create_app(settings=None):
    settings = settings or Settings()
    settings.prepare()
    vault = Vault(settings.master_key.get_secret_value())
    engine, factory = database(settings.database_url)
    with factory.begin() as db:
        try:
            for node in db.scalars(select(Node)):
                vault.decrypt(node.username_enc)
                vault.decrypt(node.password_enc)
            for workspace in db.scalars(select(Workspace)):
                vault.decrypt(workspace.key_enc)
                vault.decrypt(workspace.session_token_enc)
        except InvalidToken:
            raise RuntimeError("加密主密钥与已有数据不匹配，请恢复原 TM_MASTER_KEY") from None
        if not db.get(Account, 1):
            db.add(Account(id=1, password_hash=hash_password(settings.admin_password.get_secret_value())))
        if not db.get(SystemSettings, 1):
            db.add(
                SystemSettings(
                    id=1, request_timeout=settings.request_timeout, concurrency=settings.concurrency
                )
            )
        db.flush()
        operating = db.get(SystemSettings, 1)
        timeout, concurrency = operating.request_timeout, operating.concurrency
    services = Services(factory, Gateway(vault, timeout), concurrency)
    from backend.backups import AutoBackups

    services.backups = AutoBackups(settings)
    failures = defaultdict(list)

    @asynccontextmanager
    async def lifespan(app):
        services.recover()
        scheduler = asyncio.create_task(services.scheduler())
        yield
        scheduler.cancel()
        for task in list(services.tasks):
            task.cancel()
        await asyncio.gather(scheduler, *services.tasks, return_exceptions=True)
        engine.dispose()

    app = FastAPI(
        title="TeamManager",
        version="0.4.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services, app.state.db, app.state.vault = services, factory, vault

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "输入不符合要求，请检查填写内容",
                "fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()],
            },
        )

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": exc.public()})

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": "配置已存在或仍被其他记录引用"})

    @app.get("/api/health")
    def health():
        with factory() as db:
            version = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        maintenance = services.maintenance_status()
        healthy = maintenance["status"] != "stalled" and maintenance["failure_count"] < 3
        payload = {
            "status": "ok" if healthy else "degraded",
            "version": app.version,
            "schema": version,
            "maintenance": maintenance["status"],
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)

    def authenticated(request: Request):
        raw = request.cookies.get("tm_session", "")
        with factory() as db:
            session = db.get(Session, token_hash(raw))
            if not session or session.expires_at <= now():
                raise HTTPException(401, "请先登录")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), session.csrf
            ):
                raise HTTPException(403, "会话校验失败，请刷新后重试")
            return session

    @app.post("/api/login")
    def login(body: LoginInput, request: Request, response: Response):
        # A custom header prevents cross-origin HTML form login submissions.
        if request.headers.get("X-TeamManager") != "1":
            raise HTTPException(403, "请求来源校验失败")
        address = request.client.host if request.client else "local"
        failures[address] = [t for t in failures[address] if now() - t < 60]
        if len(failures[address]) >= 5:
            raise HTTPException(429, "登录尝试过多，请一分钟后再试")
        with factory.begin() as db:
            if not verify_password(body.password.get_secret_value(), db.get(Account, 1).password_hash):
                failures[address].append(now())
                raise HTTPException(401, "密码不正确")
            failures.pop(address, None)
            db.execute(delete(Session).where(Session.expires_at <= now()))
            raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add(Session(id=token_hash(raw), csrf=csrf, expires_at=now() + settings.session_hours * 3600))
        response.set_cookie(
            "tm_session",
            raw,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path=settings.cookie_path,
        )
        return {"csrf": csrf}

    router = APIRouter(prefix="/api", dependencies=[Depends(authenticated)])

    @router.get("/me")
    def me(session=Depends(authenticated)):
        return {"csrf": session.csrf}

    @router.get("/settings")
    def operating_settings():
        with factory() as db:
            config = db.get(SystemSettings, 1)
            return {
                "request_timeout": config.request_timeout,
                "concurrency": config.concurrency,
                "log_retention_days": config.log_retention_days,
                "version": app.version,
                "schema": db.execute(text("SELECT version_num FROM alembic_version")).scalar(),
            }

    @router.get("/maintenance")
    def maintenance():
        alerts = []
        with factory() as db:
            for workspace in db.scalars(select(Workspace).where(Workspace.enabled.is_(True))):
                if workspace.credential_type == "web_session" and workspace.auto_refresh:
                    reason = workspace.refresh_status
                    if not workspace.session_token_enc:
                        reason = "session_cookie_missing"
                    if reason in {
                        "web_verification",
                        "session_expired",
                        "session_cookie_missing",
                        "permission_denied",
                    }:
                        alerts.append(
                            {
                                "kind": "session",
                                "workspace_id": workspace.id,
                                "name": workspace.name,
                                "code": reason,
                            }
                        )
            for node in db.scalars(select(Node).where(Node.enabled.is_(True))):
                if node.health_status in {"unhealthy", "suspect"}:
                    alerts.append(
                        {"kind": "node", "node_id": node.id, "name": node.name, "code": node.health_error}
                    )
        if services.backups and services.backups.state["status"] == "failed":
            alerts.append({"kind": "backup", "name": "自动备份", "code": "backup_failed"})
        return {
            "scheduler": services.maintenance_status(),
            "alerts": alerts,
            "backup": dict(services.backups.state) if services.backups else None,
        }

    @router.post("/jobs/{id}/cancel")
    def cancel_job(id: int, body: ConfirmInput):
        if body.confirm != str(id):
            raise HTTPException(422, "请填写任务编号确认")
        services.cancel_job(id)
        return {"ok": True}

    @router.post("/jobs/{id}/confirm-invitation")
    async def confirm_invitation(id: int, body: ConfirmInput):
        if body.confirm != str(id):
            raise HTTPException(422, "请填写任务编号确认核验")
        with factory() as db:
            job = db.get(Job, id)
            if not job or job.kind != "invite" or job.status != "uncertain" or not job.workspace_id:
                raise HTTPException(409, "只能核验待确认的邀请任务")
            wid = job.workspace_id
        async with services.lock(wid), services.semaphore:
            await services.seats.refresh_locked(wid)
            with factory.begin() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                job = require(db, Job, id)
                if job.status != "uncertain":
                    raise HTTPException(409, "任务状态已变更，请刷新")
                workspace = require(db, Workspace, wid)
                hashes = set(json.loads(workspace.seat_emails_json))
                items = list(db.scalars(select(JobItem).where(JobItem.job_id == id)))
                unknown = [item for item in items if item.status == "uncertain"]
                if not unknown or any(
                    item.action != "invite" or token_hash(item.target.lower()) not in hashes
                    for item in unknown
                ):
                    raise HTTPException(409, "尚未查到对应成员或邀请，保留待核实状态")
                for item in unknown:
                    item.status = "success"
                    result = json.loads(item.result_json)
                    result["resolution"] = "observed_member_or_invitation"
                    result["message"] = "核验已发现成员或邀请"
                    item.result_json = json.dumps(result)
                job.status = "success" if all(i.status == "success" for i in items) else "failed"
                job.finished_at = now()
        services.seats.settle(id)
        services.audit("confirm_invitation", "success", job_id=id)
        return {"ok": True}

    @router.put("/settings")
    async def update_settings(body: SettingsInput):
        await services.configure(body.model_dump())
        return operating_settings()

    @router.post("/logout")
    def logout(response: Response, session=Depends(authenticated)):
        with factory.begin() as db:
            db.execute(delete(Session).where(Session.id == session.id))
        response.delete_cookie("tm_session", path=settings.cookie_path)
        return {"ok": True}

    @router.post("/password")
    def change_password(body: PasswordInput):
        with factory.begin() as db:
            account = db.get(Account, 1)
            if not verify_password(body.current_password.get_secret_value(), account.password_hash):
                raise HTTPException(400, "当前密码不正确")
            account.password_hash = hash_password(body.new_password.get_secret_value())
            db.execute(delete(Session))
        services.audit("password_changed", "success")
        return {"ok": True}

    def require(db, model, id):
        value = db.get(model, id)
        if not value:
            raise HTTPException(404, "记录不存在")
        return value

    def node_public(node):
        return {
            key: getattr(node, key)
            for key in [
                "id",
                "name",
                "scheme",
                "host",
                "port",
                "enabled",
                "note",
                "last_test_at",
                "last_test_status",
                "last_test_ip",
                "last_test_ms",
                "health_status",
                "health_checked_at",
                "health_error",
                "failure_count",
                "blocked_until",
            ]
        } | {"has_auth": bool(node.username_enc or node.password_enc)}

    def workspace_public(w, db):
        return {
            key: getattr(w, key)
            for key in [
                "id",
                "name",
                "workspace_id",
                "node_id",
                "enabled",
                "interval_minutes",
                "note",
                "last_probe_at",
                "last_status",
                "credential_type",
                "token_expires_at",
                "auto_refresh",
                "refresh_status",
                "refresh_checked_at",
                "refresh_next_at",
            ]
        } | {
            "seat_limit": w.seat_limit,
            "seats": services.seats.info(db, w),
            "has_key": bool(w.key_enc),
            "has_session_cookie": bool(w.session_token_enc),
            "capabilities": {
                "revoke": w.credential_type == "admin_api",
                "resend": True,
                "seat_type": w.credential_type == "admin_api",
            },
        }

    @router.get("/nodes")
    def nodes():
        with factory() as db:
            return [
                node_public(n)
                | {
                    "workspaces": [
                        {"id": w.id, "name": w.name}
                        for w in db.scalars(select(Workspace).where(Workspace.node_id == n.id))
                    ],
                    "workspace_count": db.scalar(
                        select(func.count()).select_from(Workspace).where(Workspace.node_id == n.id)
                    ),
                }
                for n in db.scalars(select(Node).order_by(Node.id))
            ]

    def save_node(body, id=None):
        with factory.begin() as db:
            node = require(db, Node, id) if id else Node(username_enc="", password_enc="")
            old_route = tuple(
                getattr(node, k)
                for k in ("scheme", "host", "port", "username_enc", "password_enc", "enabled")
            )
            for key in ["name", "scheme", "host", "port", "enabled", "note"]:
                setattr(node, key, getattr(body, key))
            if body.clear_auth:
                node.username_enc = node.password_enc = ""
            for source, target in [("username", "username_enc"), ("password", "password_enc")]:
                value = getattr(body, source).get_secret_value()
                if value:
                    setattr(node, target, vault.encrypt(value))
            if old_route != tuple(
                getattr(node, k)
                for k in ("scheme", "host", "port", "username_enc", "password_enc", "enabled")
            ):
                node.last_test_at, node.last_test_status = None, "not_probed"
                node.last_test_ip, node.last_test_ms = "", None
                node.health_status, node.health_error = "unknown", ""
                node.health_checked_at, node.blocked_until, node.failure_count = None, 0, 0
            db.add(node)
            db.flush()
            for bound in db.scalars(select(Workspace).where(Workspace.node_id == node.id)):
                if old_route != tuple(
                    getattr(node, k)
                    for k in ("scheme", "host", "port", "username_enc", "password_enc", "enabled")
                ):
                    bound.seat_status, bound.seat_checked_at = "not_probed", None
            result = node_public(node)
        services.audit("update_node" if id else "create_node", "success", node=node)
        return result

    @router.post("/nodes")
    def add_node(body: NodeInput):
        return save_node(body)

    @router.put("/nodes/{id}")
    def edit_node(id: int, body: NodeInput):
        return save_node(body, id)

    @router.delete("/nodes/{id}")
    def delete_node(id: int):
        with factory.begin() as db:
            node = require(db, Node, id)
            if db.scalar(select(Workspace.id).where(Workspace.node_id == id).limit(1)):
                raise HTTPException(409, "请先调整引用此节点的空间绑定")
            db.delete(node)
        services.audit("delete_node", "success", node=node)
        return {"ok": True}

    @router.post("/nodes/{id}/test")
    async def test_node(id: int):
        with factory() as db:
            node = require(db, Node, id)
        async with services.semaphore:
            import time

            started = time.monotonic()
            try:
                ip = await services.test_node(node)
                services.audit("test_node", "success", node=node)
                return {"ip": ip, "elapsed_ms": int((time.monotonic() - started) * 1000)}
            except UpstreamError as exc:
                services.audit("test_node", exc.code, node=node, error_code=exc.code)
                raise

    @router.get("/nodes/{id}/tests")
    def node_tests(id: int):
        with factory() as db:
            require(db, Node, id)
            return [
                {k: getattr(row, k) for k in ["id", "created_at", "status", "ip", "elapsed_ms"]}
                for row in db.scalars(
                    select(NodeTest).where(NodeTest.node_id == id).order_by(NodeTest.id.desc()).limit(30)
                )
            ]

    @router.get("/workspaces")
    def workspaces():
        with factory() as db:
            return [workspace_public(w, db) for w in db.scalars(select(Workspace).order_by(Workspace.id))]

    def save_workspace(body, id=None):
        with factory.begin() as db:
            node = require(db, Node, body.node_id)
            w = require(db, Workspace, id) if id else Workspace(key_enc="")
            old_type = w.credential_type
            old_binding = (w.workspace_id, w.node_id, old_type, w.key_enc, w.session_token_enc)
            for key in [
                "name",
                "workspace_id",
                "node_id",
                "enabled",
                "interval_minutes",
                "note",
                "auto_refresh",
                "seat_limit",
            ]:
                setattr(w, key, getattr(body, key))
            key = body.admin_key.get_secret_value()
            if body.credential_type == "web_session":
                try:
                    token, cookie, expiry = import_credentials(
                        body.web_access_token.get_secret_value(),
                        body.session_json.get_secret_value(),
                        body.session_token.get_secret_value(),
                        body.workspace_id,
                    )
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                if not token and (old_type != "web_session" or not w.key_enc):
                    raise HTTPException(422, "切换或新增网页模式需要导入登录会话 accessToken")
                if token:
                    w.key_enc, w.token_expires_at = vault.encrypt(token), expiry
                    # A new login must not silently reuse another login's refresh cookie.
                    w.session_token_enc = vault.encrypt(cookie) if cookie else ""
                elif cookie:
                    w.session_token_enc = vault.encrypt(cookie)
                if not token and old_binding[0] != body.workspace_id:
                    raise HTTPException(422, "更换 Workspace ID 需要重新导入目标空间的登录会话")
            elif key:
                if any(c.isspace() for c in key):
                    raise HTTPException(422, "管理员密钥不能包含空白字符")
                w.key_enc = vault.encrypt(key)
                w.session_token_enc, w.token_expires_at = "", None
            elif old_type == "web_session":
                raise HTTPException(422, "切换到 Admin API 需要填写新的 Admin key")
            if not w.key_enc:
                raise HTTPException(422, "新增空间需要填写管理员密钥")
            w.credential_type = body.credential_type
            if old_binding != (w.workspace_id, w.node_id, w.credential_type, w.key_enc, w.session_token_enc):
                if id and db.scalar(
                    select(Job.id).where(Job.workspace_id == id, Job.status.in_(["queued", "running"]))
                ):
                    raise HTTPException(409, "空间有排队或运行任务，请完成或取消后修改接入配置")
                w.seat_status, w.seat_checked_at = "not_probed", None
                w.seat_emails_json, w.member_count, w.invite_count = "[]", 0, 0
                w.last_probe_at, w.last_status = None, "not_probed"
                w.refresh_next_at, w.refresh_failures, w.refresh_status = 0, 0, "not_refreshed"
            db.add(w)
            db.flush()
            result = workspace_public(w, db)
        services.audit("update_workspace" if id else "create_workspace", "success", w, node)
        return result

    @router.post("/workspaces")
    def add_workspace(body: WorkspaceInput):
        return save_workspace(body)

    @router.put("/workspaces/{id}")
    def edit_workspace(id: int, body: WorkspaceInput):
        return save_workspace(body, id)

    @router.post("/workspaces/{id}/refresh-seats")
    async def refresh_seats(id: int):
        with factory() as db:
            require(db, Workspace, id)
        return await services.seats.refresh(id)

    @router.get("/workspaces/{id}/reservations")
    def seat_reservations(id: int):
        with factory() as db:
            require(db, Workspace, id)
            return [
                {
                    "id": r.id,
                    "job_item_id": r.job_item_id,
                    "state": r.state,
                    "created_at": r.created_at,
                    "job_id": item.job_id if item else None,
                    "email": item.target if item else "",
                }
                for r, item in db.execute(
                    select(SeatReservation, JobItem)
                    .outerjoin(JobItem, JobItem.id == SeatReservation.job_item_id)
                    .where(
                        SeatReservation.workspace_id == id,
                        SeatReservation.state.in_(["reserved", "submitted", "uncertain"]),
                    )
                )
            ]

    @router.post("/reservations/{id}/release")
    def release_reservation(id: int, body: ConfirmInput):
        if body.confirm != str(id):
            raise HTTPException(422, "请填写预留编号确认，且先在官方管理台核实邀请未占用席位")
        with factory.begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            reservation = require(db, SeatReservation, id)
            item = db.get(JobItem, reservation.job_item_id) if reservation.job_item_id else None
            job = db.get(Job, item.job_id) if item else None
            if job and job.status in {"queued", "running"}:
                raise HTTPException(409, "邀请任务尚未结束，不能释放预留")
            if job and job.status == "uncertain" and item and item.status == "uncertain":
                item.status = "failed"
                result = json.loads(item.result_json)
                result["resolution"] = "admin_confirmed_not_sent"
                item.result_json = json.dumps(result)
                db.flush()
                unknown = db.scalar(
                    select(JobItem.id).where(JobItem.job_id == job.id, JobItem.status == "uncertain")
                )
                if not unknown:
                    job.status, job.finished_at = "failed", now()
            reservation.state = "released"
        services.audit("release_seat_reservation", "success", target=str(id))
        return {"ok": True}

    @router.post("/workspaces/{id}/refresh-session")
    async def refresh_web_session(id: int):
        async with services.lock(id), services.semaphore:
            w, node = services.pair(id)
            if not w.enabled:
                raise UpstreamError("workspace_disabled")
            async with services.gateway.node_locks.setdefault(node.id, asyncio.Lock()):
                return await services.automation.refresh(w, node)

    @router.delete("/workspaces/{id}")
    def delete_workspace(id: int, body: ConfirmInput):
        with factory.begin() as db:
            w = require(db, Workspace, id)
            if body.confirm != w.name:
                raise HTTPException(400, "空间名称不匹配")
            if (
                id in services.pending_probes
                or services.lock(id).locked()
                or db.scalar(
                    select(Job.id)
                    .where(Job.workspace_id == id, Job.status.in_(["queued", "running"]))
                    .limit(1)
                )
            ):
                raise HTTPException(409, "空间仍有任务运行，请稍后再试")
            db.delete(w)
        services.audit("delete_workspace", "success", workspace=w)
        return {"ok": True}

    @router.post("/workspaces/{id}/probe")
    async def probe(id: int):
        with factory() as db:
            require(db, Workspace, id)
        if id in services.pending_probes or services.lock(id).locked():
            raise HTTPException(409, "该空间已有请求正在执行")
        return await services.probe(id)

    @router.post("/probe-all")
    async def probe_all():
        with factory() as db:
            ids = list(db.scalars(select(Workspace.id).where(Workspace.enabled.is_(True))))
        ids = [id for id in ids if not services.lock(id).locked()]
        job_ids = [job_id for id in ids if (job_id := services.schedule_probe(id)) is not None]
        return {"scheduled": len(job_ids), "job_ids": job_ids}

    @router.get("/workspaces/{id}/probes")
    def probes(id: int):
        with factory() as db:
            return [
                {
                    "id": p.id,
                    "created_at": p.created_at,
                    "status": p.status,
                    "node_name": p.node_name,
                    "details": json.loads(p.details_json),
                    "elapsed_ms": p.elapsed_ms,
                }
                for p in db.scalars(
                    select(Probe).where(Probe.workspace_id == id).order_by(Probe.id.desc()).limit(30)
                )
            ]

    @router.get("/workspaces/{id}/{resource}")
    async def list_resources(id: int, resource: str, after: str | None = Query(None, max_length=256)):
        if resource not in {"users", "invites"}:
            raise HTTPException(404)
        with factory() as db:
            require(db, Workspace, id)
        return await services.page(id, resource, after)

    @router.get("/workspaces/{id}/{resource}/{target}")
    async def resource_detail(id: int, resource: str, target: str):
        if resource not in {"users", "invites"} or len(target) > 256:
            raise HTTPException(404)
        return await services.detail(id, resource, target)

    @router.post("/workspaces/{id}/invite")
    async def invite(id: int, body: InviteInput):
        with factory() as db:
            workspace = require(db, Workspace, id)
            if workspace.credential_type == "web_session" and body.seat_type:
                raise UpstreamError("operation_unsupported")
        if workspace.seat_limit > 0:
            await services.seats.refresh(id, force=False)
        return {"job_id": services.create_job(id, "invite", [str(e) for e in body.emails], body.seat_type)}

    @router.post("/workspaces/{id}/{action}/{target}")
    async def mutate(id: int, action: str, target: str, body: ConfirmInput):
        if action not in {"remove", "revoke", "resend"} or len(target) > 256:
            raise HTTPException(404)
        resource = "users" if action == "remove" else "invites"
        with factory() as db:
            w = require(db, Workspace, id)
            if w.credential_type == "web_session" and action == "revoke":
                raise UpstreamError("operation_unsupported")
        record = await services.detail(id, resource, target)
        expected = record.get("email") or target
        if body.confirm != expected:
            raise HTTPException(400, "确认邮箱或目标 ID 不匹配")
        if record.get("is_scim_managed") or record.get("role") in {"owner", "admin"}:
            raise HTTPException(409, "请在官方管理台处理管理员、所有者或目录同步对象")
        if w.credential_type == "web_session" and action == "remove" and record.get("role") != "member":
            raise HTTPException(409, "无法确认此成员的角色，请先到官方管理台核实")
        return {"job_id": services.create_job(id, action, [target])}

    @router.get("/jobs")
    def jobs(before: int | None = None):
        with factory() as db:
            query = select(Job).order_by(Job.id.desc()).limit(50)
            if before:
                query = query.where(Job.id < before)
            return [
                {
                    "id": j.id,
                    "workspace_id": j.workspace_id,
                    "workspace_name": j.workspace_name,
                    "queue_position": db.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(Job.kind.in_(["invite", "resend"]), Job.status == "queued", Job.id <= j.id)
                    )
                    if j.status == "queued" and j.kind in {"invite", "resend"}
                    else None,
                    "kind": j.kind,
                    "status": j.status,
                    "created_at": j.created_at,
                    "started_at": j.started_at,
                    "finished_at": j.finished_at,
                    "items": [
                        {
                            "id": i.id,
                            "target": i.target,
                            "action": i.action,
                            "status": i.status,
                            "result": json.loads(i.result_json),
                        }
                        for i in db.scalars(
                            select(JobItem).where(JobItem.job_id == j.id).order_by(JobItem.id)
                        )
                    ],
                }
                for j in db.scalars(query)
            ]

    @router.get("/audit")
    def audit(before: int | None = None, workspace_id: int | None = None):
        with factory() as db:
            query = select(Audit).order_by(Audit.id.desc()).limit(100)
            if before:
                query = query.where(Audit.id < before)
            if workspace_id is not None:
                query = query.where(Audit.workspace_id == workspace_id)
            return [
                {
                    k: getattr(a, k)
                    for k in [
                        "id",
                        "created_at",
                        "workspace_id",
                        "workspace_name",
                        "node_name",
                        "action",
                        "target",
                        "status",
                        "job_id",
                        "error_code",
                        "request_id",
                    ]
                }
                for a in db.scalars(query)
            ]

    from backend.redemption import mount_redemption

    mount_redemption(app, router, services, settings.public_redemption)
    app.include_router(router)
    if settings.frontend_dir.exists():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="assets")
        app.mount("/TeamManager/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="shared-assets")

    @app.get("/{path:path}")
    def index(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        if not (settings.frontend_dir / "index.html").is_file():
            return JSONResponse({"message": "请先构建前端，参阅 README"}, status_code=503)
        return FileResponse(settings.frontend_dir / "index.html")

    return app
