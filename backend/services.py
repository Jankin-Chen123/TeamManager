import asyncio
import json
import time
from urllib.parse import quote

from sqlalchemy import delete, select, update, text
from fastapi import HTTPException

from backend.models import (
    Audit,
    Job,
    JobItem,
    Node,
    NodeTest,
    Probe,
    Workspace,
    SystemSettings,
    Redemption,
    SeatReservation,
    now,
)
from backend.limiter import ConcurrencyGate
from backend.upstream import UpstreamError, clean_page, clean_record


class Services:
    def __init__(self, factory, gateway, concurrency):
        self.db, self.gateway = factory, gateway
        self.semaphore = ConcurrencyGate(concurrency)
        self.locks = {}
        self.tasks = set()
        self.pending_probes = set()
        self.invitation_lock = asyncio.Lock()
        self.backups = None
        self.last_cleanup = 0
        self.maintenance = {
            "started_at": now(),
            "last_background_failure_at": None,
            "status": "starting",
            "last_tick_at": None,
            "last_success_at": None,
            "failure_count": 0,
            "background_failures": 0,
        }
        from backend.automation import Automation

        self.automation = Automation(self)
        from backend.seats import Seats

        self.seats = Seats(self)
        gateway.prepare, gateway.observe = self.automation.prepare, self.automation.observe

    async def configure(self, values):
        with self.db.begin() as db:
            stored = db.get(SystemSettings, 1)
            for key, value in values.items():
                setattr(stored, key, value)
        self.gateway.timeout = values["request_timeout"]
        await self.semaphore.resize(values["concurrency"])
        self.cleanup_logs()
        self.audit("update_settings", "success")

    def cleanup_logs(self):
        with self.db.begin() as db:
            config = db.get(SystemSettings, 1)
            if config and config.log_retention_days:
                cutoff = now() - config.log_retention_days * 86400
                db.execute(delete(Audit).where(Audit.created_at < cutoff))
                db.execute(delete(Probe).where(Probe.created_at < cutoff))
                db.execute(delete(NodeTest).where(NodeTest.created_at < cutoff))
                old_jobs = select(Job.id).where(
                    Job.finished_at < cutoff,
                    Job.status.in_(["success", "failed", "cancelled"]),
                    ~Job.id.in_(select(Redemption.job_id).where(Redemption.job_id.is_not(None))),
                    ~Job.id.in_(
                        select(JobItem.job_id)
                        .join(SeatReservation, SeatReservation.job_item_id == JobItem.id)
                        .where(SeatReservation.state.in_(["reserved", "submitted", "uncertain"]))
                    ),
                )
                db.execute(delete(JobItem).where(JobItem.job_id.in_(old_jobs)))
                db.execute(delete(Job).where(Job.id.in_(old_jobs)))
        self.last_cleanup = now()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        # Consume unexpected exceptions without logging secret-bearing exception text.
        def observe(task):
            if not task.cancelled() and task.exception() is not None:
                self.maintenance["background_failures"] += 1
                self.maintenance["last_background_failure_at"] = now()

        task.add_done_callback(observe)
        return task

    def lock(self, workspace_id):
        return self.locks.setdefault(workspace_id, asyncio.Lock())

    def recover(self):
        with self.db.begin() as db:
            queued_public = list(
                db.scalars(
                    select(Job.id)
                    .where(Job.status == "queued", Job.kind.in_(["invite", "resend"]))
                    .order_by(Job.id)
                )
            )
            # Never replay writes left in flight by a crash or an upgrade.
            db.execute(
                update(JobItem)
                .where(JobItem.status == "running", JobItem.action != "probe")
                .values(status="uncertain")
            )
            db.execute(
                update(JobItem)
                .where(JobItem.status == "running", JobItem.action == "probe")
                .values(status="failed", result_json=json.dumps({"message": "程序中断，探测未完成"}))
            )
            db.execute(
                update(JobItem)
                .where(JobItem.status == "queued", ~JobItem.job_id.in_(queued_public))
                .values(status="cancelled")
            )
            for job in db.scalars(select(Job).where(Job.status.in_(["running", "queued"]))):
                if job.id in queued_public:
                    continue
                uncertain = db.scalar(
                    select(JobItem.id).where(JobItem.job_id == job.id, JobItem.status == "uncertain")
                )
                job.status = "uncertain" if uncertain else ("failed" if job.kind == "probe" else "cancelled")
                job.finished_at = now()
        with self.db() as db:
            recovered_ids = list(db.scalars(select(Job.id)))
        for job_id in recovered_ids:
            self.seats.settle(job_id)
        for job_id in queued_public:
            self.spawn(self.run_job(job_id))

    def pair(self, workspace_id):
        with self.db() as db:
            workspace = db.get(Workspace, workspace_id)
            if not workspace:
                raise UpstreamError("unavailable")
            node = db.get(Node, workspace.node_id)
            return workspace, node

    def audit(
        self,
        action,
        status,
        workspace=None,
        node=None,
        target="",
        *,
        job_id=None,
        error_code="",
        request_id="",
    ):
        with self.db.begin() as db:
            db.add(
                Audit(
                    action=action,
                    status=status,
                    target=target,
                    workspace_name=workspace.name if workspace else "",
                    node_name=node.name if node else "",
                    workspace_id=workspace.id if workspace and db.get(Workspace, workspace.id) else None,
                    job_id=job_id,
                    error_code=error_code,
                    request_id=request_id,
                )
            )

    async def page(self, workspace_id, resource, after=None):
        async with self.lock(workspace_id), self.semaphore:
            workspace, node = self.pair(workspace_id)
            params = {"limit": 100}
            if after:
                params["after"] = after
            try:
                raw = await self.gateway.workspace(workspace, node, "GET", resource, params=params)
                data = clean_page(raw, resource)
                self.audit(
                    f"list_{resource}", "success", workspace, node, request_id=getattr(raw, "request_id", "")
                )
                return data
            except UpstreamError as exc:
                self.audit(
                    f"list_{resource}",
                    exc.code,
                    workspace,
                    node,
                    error_code=exc.code,
                    request_id=exc.request_id,
                )
                raise

    async def detail(self, workspace_id, resource, target):
        async with self.lock(workspace_id), self.semaphore:
            workspace, node = self.pair(workspace_id)
            try:
                data = await self.gateway.workspace(
                    workspace, node, "GET", f"{resource}/{quote(target, safe='')}"
                )
                self.audit(
                    f"get_{resource}",
                    "success",
                    workspace,
                    node,
                    target,
                    request_id=getattr(data, "request_id", ""),
                )
                return clean_record(data, resource)
            except UpstreamError as exc:
                self.audit(
                    f"get_{resource}",
                    exc.code,
                    workspace,
                    node,
                    target,
                    error_code=exc.code,
                    request_id=exc.request_id,
                )
                raise

    async def test_node(self, node):
        started = time.monotonic()
        ip, status = "", "failed"
        try:
            ip = await self.gateway.test_node(node)
            status = "success"
            return ip
        except UpstreamError as exc:
            status = exc.code
            raise
        finally:
            self.automation.report_node(node, status)
            with self.db.begin() as db:
                live = db.get(Node, node.id)
                if live:
                    elapsed = int((time.monotonic() - started) * 1000)
                    db.add(NodeTest(node_id=node.id, status=status, ip=ip, elapsed_ms=elapsed))
                    if all(
                        getattr(live, k) == getattr(node, k)
                        for k in ("scheme", "host", "port", "username_enc", "password_enc", "enabled")
                    ):
                        live.last_test_at, live.last_test_status = now(), status
                        live.last_test_ip, live.last_test_ms = ip, elapsed

    def prepare_probe(self, workspace_id):
        if workspace_id in self.pending_probes:
            return None
        workspace, _ = self.pair(workspace_id)
        with self.db.begin() as db:
            job = Job(workspace_id=workspace_id, workspace_name=workspace.name, kind="probe")
            db.add(job)
            db.flush()
            db.add(JobItem(job_id=job.id, target=workspace.workspace_id, action="probe"))
            job_id = job.id
        self.pending_probes.add(workspace_id)
        return job_id

    def schedule_probe(self, workspace_id):
        job_id = self.prepare_probe(workspace_id)
        if job_id is not None:
            self.spawn(self.probe(workspace_id, job_id))
        return job_id

    async def probe(self, workspace_id, job_id=None):
        job_id = job_id if job_id is not None else self.prepare_probe(workspace_id)
        if job_id is None:
            raise UpstreamError("operation_busy")
        try:
            result = await self._probe(workspace_id, job_id)
            with self.db.begin() as db:
                job = db.get(Job, job_id)
                job.status = "success" if result["status"] == "success" else "failed"
                job.finished_at = now()
                item = db.scalar(select(JobItem).where(JobItem.job_id == job_id))
                item.status, item.result_json = job.status, json.dumps(result)
            return result | {"job_id": job_id}
        except BaseException:
            with self.db.begin() as db:
                job = db.get(Job, job_id)
                job.status, job.finished_at = "failed", now()
                item = db.scalar(select(JobItem).where(JobItem.job_id == job_id))
                item.status = "failed"
                item.result_json = json.dumps({"message": "探测中断或执行失败，请重新探测"})
            raise
        finally:
            self.pending_probes.discard(workspace_id)

    async def _probe(self, workspace_id, job_id):
        async with self.lock(workspace_id), self.semaphore:
            started = time.monotonic()
            with self.db.begin() as db:
                job = db.get(Job, job_id)
                job.status, job.started_at = "running", now()
                db.scalar(select(JobItem).where(JobItem.job_id == job_id)).status = "running"
            workspace, node = self.pair(workspace_id)
            results = {}
            if not workspace.enabled:
                results["gateway"] = {"code": "workspace_disabled"}
            else:
                try:
                    ip = await self.test_node(node)
                    results["node"] = {"code": "success", "ip": ip}
                except UpstreamError as exc:
                    results["node"] = exc.public()
                # An IP lookup service failing does not prove the management API is unreachable.
                try:
                    await self.seats.refresh_locked(workspace_id, diagnostics=results)
                except UpstreamError:
                    # Collection already recorded each endpoint failure and invalidated the seat snapshot.
                    pass
            codes = [r["code"] for r in results.values()]
            status = (
                "success"
                if all(c == "success" for c in codes)
                else ("partial" if "success" in codes else codes[0])
            )
            with self.db.begin() as db:
                live = db.get(Workspace, workspace_id)
                live.last_probe_at, live.last_status = now(), status
                probe = Probe(
                    workspace_id=workspace_id,
                    node_name=node.name,
                    status=status,
                    details_json=json.dumps(results),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                db.add(probe)
                db.flush()
                result = {
                    "id": probe.id,
                    "status": status,
                    "details": results,
                    "elapsed_ms": probe.elapsed_ms,
                }
            for resource, outcome in results.items():
                self.audit(
                    "probe_" + resource,
                    outcome["code"],
                    workspace,
                    node,
                    job_id=job_id,
                    error_code="" if outcome["code"] == "success" else outcome["code"],
                    request_id=outcome.get("request_id", ""),
                )
            return result

    def create_job(self, workspace_id, action, targets, seat_type=None):
        workspace, node = self.pair(workspace_id)
        if not workspace.enabled:
            raise UpstreamError("workspace_disabled")
        if not node.enabled:
            raise UpstreamError("proxy_disabled")
        with self.db.begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            workspace = db.get(Workspace, workspace_id)
            targets = list(dict.fromkeys(str(t).lower() if action == "invite" else str(t) for t in targets))
            if action == "invite" and workspace.seat_limit > 0:
                if self.seats.info(db, workspace)["available"] < len(targets):
                    raise HTTPException(409, "已确认的有效席位不足，请刷新席位后再试")
                from backend.security import token_hash

                occupied = set(json.loads(workspace.seat_emails_json))
                held = set(
                    db.scalars(
                        select(SeatReservation.email_hash).where(
                            SeatReservation.state.in_(["reserved", "submitted", "uncertain"])
                        )
                    )
                )
                if any(token_hash(t) in occupied | held for t in targets):
                    raise HTTPException(409, "邮箱已有成员、邀请或待处理申请")
            job = Job(workspace_id=workspace_id, workspace_name=workspace.name, kind=action)
            db.add(job)
            db.flush()
            for target in dict.fromkeys(targets):
                item = JobItem(
                    job_id=job.id,
                    target=str(target),
                    action=action,
                    result_json=json.dumps({"seat_type": seat_type}),
                )
                db.add(item)
                db.flush()
                if action == "invite":
                    self.seats.reserve(db, workspace_id, item, str(target).lower())
            job_id = job.id
        self.spawn(self.run_job(job_id))
        return job_id

    async def run_job(self, job_id):
        with self.db() as db:
            job = db.get(Job, job_id)
            serial = job is not None and job.kind in {"invite", "resend"}
        if serial:
            async with self.invitation_lock:
                await self._run_job(job_id)
        else:
            await self._run_job(job_id)

    async def _run_job(self, job_id):
        with self.db.begin() as db:
            claimed = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="running", started_at=now())
            )
            if claimed.rowcount != 1:
                return
            job = db.get(Job, job_id)
            workspace_id = job.workspace_id
            ids = list(db.scalars(select(JobItem.id).where(JobItem.job_id == job_id).order_by(JobItem.id)))
        try:
            async with self.lock(workspace_id), self.semaphore:
                for item_id in ids:
                    # Refresh bindings for each request; in-flight requests finish on their original node.
                    workspace, node = self.pair(workspace_id)
                    with self.db.begin() as db:
                        item = db.get(JobItem, item_id)
                        action, target = item.action, item.target
                        seat_type = json.loads(item.result_json).get("seat_type")
                        item.status = "running"
                    resource, body = "invites", None
                    if action == "invite":
                        method = "POST"
                        body = {"email": target, "role": "member", "seat_type": seat_type}
                    elif action == "resend":
                        method, resource = "POST", f"invites/{quote(target, safe='')}/resend"
                    else:
                        method = "DELETE"
                        resource = f"{'users' if action == 'remove' else 'invites'}/{quote(target, safe='')}"
                    write_started = False
                    try:
                        if action == "invite":
                            await self.seats.check_invite_locked(workspace_id, item_id, target)
                            workspace, node = self.pair(workspace_id)
                        write_started = True
                        raw = await self.gateway.workspace(workspace, node, method, resource, body=body)
                        result = clean_record(raw, "users" if action == "remove" else "invites")
                        result["request_id"] = getattr(raw, "request_id", "")
                        status = "success"
                    except UpstreamError as exc:
                        result = exc.public()
                        status = "uncertain" if exc.uncertain and write_started else "failed"
                        if not write_started:
                            result["uncertain"] = False
                    with self.db.begin() as db:
                        item = db.get(JobItem, item_id)
                        item.status, item.result_json = status, json.dumps(result)
                    self.audit(
                        action,
                        status,
                        workspace,
                        node,
                        target,
                        job_id=job_id,
                        error_code=result.get("code", ""),
                        request_id=result.get("request_id", ""),
                    )
                    if result.get("code") in {
                        "seats_full",
                        "rate_limited",
                        "invalid_key",
                        "session_expired",
                        "wrong_credential",
                        "web_verification",
                        "operation_unsupported",
                        "permission_denied",
                        "proxy_disabled",
                        "workspace_disabled",
                        "proxy_connection",
                        "proxy_auth",
                        "tls_error",
                        "node_cooling",
                    }:
                        # Stop the batch instead of hammering a broken route or exhausted quota.
                        with self.db.begin() as db:
                            db.execute(
                                update(JobItem)
                                .where(JobItem.job_id == job_id, JobItem.status == "queued")
                                .values(
                                    status="cancelled",
                                    result_json=json.dumps({"message": "前项失败，剩余任务已停止"}),
                                )
                            )
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            with self.db.begin() as db:
                db.execute(
                    update(JobItem)
                    .where(JobItem.job_id == job_id, JobItem.status == "running")
                    .values(status="uncertain")
                )
                db.execute(
                    update(JobItem)
                    .where(JobItem.job_id == job_id, JobItem.status == "queued")
                    .values(status="cancelled")
                )
        finally:
            with self.db.begin() as db:
                db.execute(
                    update(JobItem)
                    .where(JobItem.job_id == job_id, JobItem.status == "running")
                    .values(status="uncertain")
                )
                db.execute(
                    update(JobItem)
                    .where(JobItem.job_id == job_id, JobItem.status == "queued")
                    .values(status="cancelled")
                )
                states = list(db.scalars(select(JobItem.status).where(JobItem.job_id == job_id)))
                job = db.get(Job, job_id)
                job.status = (
                    "uncertain"
                    if "uncertain" in states
                    else ("success" if states and all(s == "success" for s in states) else "failed")
                )
                job.finished_at = now()

            self.seats.settle(job_id)

    def maintenance_status(self):
        result = dict(self.maintenance)
        if now() - (result["last_tick_at"] or result["started_at"]) > 60:
            result["status"] = "stalled"
        elif result["last_background_failure_at"] and now() - result["last_background_failure_at"] < 300:
            result["status"] = "degraded"
        return result

    def cancel_job(self, job_id):
        with self.db.begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(Job, job_id)
            if not job:
                raise HTTPException(404, "任务不存在")
            if job.status != "queued":
                raise HTTPException(409, "只能取消尚未开始的任务")
            job.status, job.finished_at = "cancelled", now()
            db.execute(
                update(JobItem)
                .where(JobItem.job_id == job_id, JobItem.status == "queued")
                .values(status="cancelled")
            )
        self.seats.settle(job_id)
        self.audit("cancel_job", "success", job_id=job_id)

    async def maintenance_tick(self):
        if self.backups:
            self.backups.tick(self)
        self.automation.tick()
        if now() - self.last_cleanup >= 3600:
            self.cleanup_logs()
        with self.db() as db:
            due = [
                w.id
                for w in db.scalars(select(Workspace).where(Workspace.enabled.is_(True)))
                if w.interval_minutes
                and (not w.last_probe_at or now() - w.last_probe_at >= w.interval_minutes * 60)
            ]
        for workspace_id in due:
            if workspace_id in self.pending_probes or self.lock(workspace_id).locked():
                continue
            self.schedule_probe(workspace_id)
        # A scheduled full probe already collects seats. Register it first so
        # the seat refresher does not enqueue another scan of the same space.
        self.seats.tick()

    async def scheduler(self):
        while True:
            self.maintenance["last_tick_at"] = now()
            try:
                await self.maintenance_tick()
                self.maintenance.update(status="healthy", last_success_at=now(), failure_count=0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never include exception text; it can contain proxy/session credentials.
                self.maintenance["status"] = "degraded"
                self.maintenance["failure_count"] += 1
            await asyncio.sleep(10)
