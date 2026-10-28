"""One-use CDKs reserve an invitation job in the same database transaction."""

import json
import secrets
import time
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, text

from backend.models import CDK, Redemption, Job, JobItem, Node, SeatReservation, now
from backend.schemas import CDKInput, ConfirmInput, RedeemInput, RedemptionStatusInput
from backend.security import token_hash


def mount_redemption(app, admin, services, enabled):
    factory, vault = services.db, services.gateway.vault
    public = APIRouter(prefix="/api/public")
    # Bounded per-process limits; never trust arbitrary X-Forwarded-For headers.
    attempts = OrderedDict()
    global_attempts = []
    availability_cache = None
    availability_cached_at = 0.0

    def limit(request):
        stamp = time.monotonic()
        address = token_hash(request.client.host if request.client else "unknown")
        entries = [t for t in attempts.pop(address, []) if stamp - t < 60]
        global_attempts[:] = [t for t in global_attempts if stamp - t < 60]
        if len(entries) >= 20 or len(global_attempts) >= 300:
            attempts[address] = entries
            raise HTTPException(429, "请求过于频繁，请稍后再试", headers={"Retry-After": "60"})
        attempts[address] = entries + [stamp]
        global_attempts.append(stamp)
        while len(attempts) > 10000:
            attempts.popitem(last=False)

    def receipt_result(db, redemption):
        job = db.get(Job, redemption.job_id) if redemption.job_id else None
        state = job.status if job else "uncertain"
        messages = {
            "queued": "核验通过，邀请正在排队。",
            "running": "核验通过，正在发送邀请。",
            "success": "邀请已提交，请检查该邮箱的收件箱和垃圾邮件；接受邀请后才会加入空间。",
            "failed": "邀请未完成，请联系管理员核查；重复兑换不会重新发送。",
            "uncertain": "发送结果待核实，请联系管理员，勿重复申请或使用新的兑换码。",
            "cancelled": "邀请已停止，请联系管理员。",
        }
        return {"status": state, "message": messages.get(state, messages["uncertain"])}

    @admin.post("/cdks")
    def create_codes(body: CDKInput):
        if body.expires_at and body.expires_at <= now():
            raise HTTPException(422, "兑换截止时间必须晚于当前时间")
        codes = []
        with factory.begin() as db:
            for _ in range(body.count):
                code = "TM-" + secrets.token_hex(20).upper()
                db.add(
                    CDK(
                        code_hash=token_hash(code),
                        workspace_id=None,
                        label=body.label,
                        expires_at=body.expires_at,
                        bound_email=str(body.bound_email).lower() if body.bound_email else "",
                    )
                )
                codes.append(code)
        services.audit("create_cdks", "success", target=f"count:{len(codes)}")
        return {"codes": codes, "message": "兑换码仅本次展示，请保存后分发。"}

    @admin.get("/cdks")
    def list_codes():
        with factory() as db:
            rows = db.execute(
                select(CDK, Redemption, Job)
                .outerjoin(Redemption, Redemption.cdk_id == CDK.id)
                .outerjoin(Job, Job.id == Redemption.job_id)
                .order_by(CDK.id.desc())
                .limit(500)
            ).all()
            return [
                {
                    "id": c.id,
                    "label": c.label,
                    "workspace_id": r.workspace_id if r else None,
                    "enabled": c.enabled,
                    "created_at": c.created_at,
                    "expires_at": c.expires_at,
                    "bound_email": c.bound_email,
                    "email": r.email if r else "",
                    "job_id": r.job_id if r else None,
                    "status": (j.status if j else "uncertain")
                    if r
                    else (
                        "revoked"
                        if not c.enabled
                        else "expired"
                        if c.expires_at and c.expires_at <= now()
                        else "available"
                    ),
                }
                for c, r, j in rows
            ]

    @admin.post("/cdks/{id}/revoke")
    def revoke_code(id: int, body: ConfirmInput):
        if body.confirm != str(id):
            raise HTTPException(422, "请填写兑换码编号确认")
        with factory.begin() as db:
            code = db.get(CDK, id)
            if not code:
                raise HTTPException(404, "兑换码不存在")
            if db.scalar(select(Redemption.id).where(Redemption.cdk_id == id)):
                raise HTTPException(409, "已兑换的码不能撤销邀请，请在任务中心核查")
            code.enabled = False
        services.audit("revoke_cdk", "success", target=str(id))
        return {"ok": True}

    @admin.post("/cdks/{id}/retry")
    async def retry_redemption(id: int, body: ConfirmInput):
        if body.confirm != str(id):
            raise HTTPException(422, "请填写兑换码编号确认补发")

        def candidate(db):
            code = db.get(CDK, id)
            redemption = db.scalar(select(Redemption).where(Redemption.cdk_id == id))
            job = db.get(Job, redemption.job_id) if redemption and redemption.job_id else None
            if not code or not redemption or not job:
                raise HTTPException(404, "兑换申请不存在")
            if not code.enabled or (code.expires_at and code.expires_at <= now()):
                raise HTTPException(409, "兑换码已停用或过期")
            if job.status not in {"failed", "cancelled"}:
                raise HTTPException(409, "只允许补发明确失败或未发送的申请；待核实结果需先处理预留")
            items = list(db.scalars(select(JobItem).where(JobItem.job_id == job.id)))
            if len(items) != 1 or items[0].status not in {"failed", "cancelled"}:
                raise HTTPException(409, "原任务尚未明确结束")
            if db.scalar(
                select(SeatReservation.id).where(
                    SeatReservation.job_item_id == items[0].id,
                    SeatReservation.state.in_(["reserved", "submitted", "uncertain"]),
                )
            ):
                raise HTTPException(409, "原任务仍占有预留，请先核实")
            return redemption, job

        async with services.seats.allocation_lock:
            with factory() as db:
                candidate(db)
            await services.seats.ensure_pool()
            with factory.begin() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                redemption, old_job = candidate(db)
                old_job_id = old_job.id
                workspace = services.seats.choose(db, redemption.email)
                node = db.get(Node, workspace.node_id)
                job = Job(workspace_id=workspace.id, workspace_name=workspace.name, kind="invite")
                db.add(job)
                db.flush()
                item = JobItem(
                    job_id=job.id,
                    action="invite",
                    target=redemption.email,
                    result_json=json.dumps({"seat_type": None}),
                )
                db.add(item)
                db.flush()
                services.seats.reserve(db, workspace.id, item, redemption.email)
                redemption.job_id, redemption.workspace_id = job.id, workspace.id
                job_id = job.id
            services.audit(
                "retry_redemption",
                "success",
                workspace,
                node,
                target=f"cdk:{id};previous_job:{old_job_id}",
                job_id=job_id,
            )
            services.spawn(services.run_job(job_id))
        return {"job_id": job_id}

    @public.get("/availability")
    async def availability(request: Request):
        nonlocal availability_cache, availability_cached_at
        if not enabled:
            raise HTTPException(404)
        limit(request)
        stamp = time.monotonic()
        if availability_cache is None or stamp - availability_cached_at >= 3:
            availability_cache = services.seats.summary()
            availability_cached_at = stamp
        return availability_cache

    @public.post("/redeem")
    async def redeem(body: RedeemInput, request: Request):
        if not enabled:
            raise HTTPException(404)
        if request.headers.get("X-TeamManager") != "1":
            raise HTTPException(403, "请求来源校验失败")
        limit(request)
        code_hash = token_hash(body.code.get_secret_value().strip().upper())
        email = str(body.email).lower()
        job_id = None
        async with services.seats.allocation_lock:
            # Validate first: invalid or already redeemed codes never trigger upstream reads.
            with factory() as db:
                code = db.scalar(select(CDK).where(CDK.code_hash == code_hash))
                if not code:
                    raise HTTPException(400, "兑换码不可用或核验未通过")
                previous = db.scalar(select(Redemption).where(Redemption.cdk_id == code.id))
                if previous:
                    if previous.email != email:
                        raise HTTPException(400, "兑换码不可用或核验未通过")
                    return receipt_result(db, previous) | {"receipt": vault.decrypt(previous.receipt_enc)}
                if (
                    not code.enabled
                    or (code.expires_at and code.expires_at <= now())
                    or (code.bound_email and code.bound_email != email)
                ):
                    raise HTTPException(400, "兑换码不可用或核验未通过")
            await services.seats.ensure_pool()
            with factory.begin() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                code = db.scalar(select(CDK).where(CDK.code_hash == code_hash))
                if not code:
                    raise HTTPException(400, "兑换码不可用或核验未通过")
                redemption = db.scalar(select(Redemption).where(Redemption.cdk_id == code.id))
                if redemption:
                    if redemption.email != email:
                        raise HTTPException(400, "兑换码不可用或核验未通过")
                    return receipt_result(db, redemption) | {"receipt": vault.decrypt(redemption.receipt_enc)}
                if (
                    not code.enabled
                    or (code.expires_at and code.expires_at <= now())
                    or (code.bound_email and code.bound_email != email)
                ):
                    raise HTTPException(400, "兑换码不可用或核验未通过")
                workspace = services.seats.choose(db, email)
                node = db.get(Node, workspace.node_id)
                job = Job(workspace_id=workspace.id, workspace_name=workspace.name, kind="invite")
                db.add(job)
                db.flush()
                item = JobItem(
                    job_id=job.id, action="invite", target=email, result_json=json.dumps({"seat_type": None})
                )
                db.add(item)
                db.flush()
                services.seats.reserve(db, workspace.id, item, email)
                receipt = secrets.token_urlsafe(32)
                redemption = Redemption(
                    workspace_id=workspace.id,
                    cdk_id=code.id,
                    email=email,
                    receipt_hash=token_hash(receipt),
                    receipt_enc=vault.encrypt(receipt),
                    job_id=job.id,
                )
                db.add(redemption)
                db.flush()
                job_id = job.id
                result = receipt_result(db, redemption) | {"receipt": receipt}
            services.audit("redeem_cdk", "success", workspace, node, target=email, job_id=job_id)
            services.spawn(services.run_job(job_id))
        return result

    @public.post("/redemption-status")
    async def redemption_status(body: RedemptionStatusInput, request: Request):
        if not enabled:
            raise HTTPException(404)
        limit(request)
        receipt = body.receipt.get_secret_value()
        with factory() as db:
            redemption = db.scalar(select(Redemption).where(Redemption.receipt_hash == token_hash(receipt)))
            if not redemption:
                raise HTTPException(404, "查询凭证无效")
            return receipt_result(db, redemption)

    app.include_router(public)
