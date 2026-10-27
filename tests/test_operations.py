import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from backend.app import create_app
from backend.db import database
from backend.limiter import ConcurrencyGate
from backend.models import Audit, Job, JobItem, Node, NodeTest, SystemSettings, now
from backend.upstream import GatewayResult, UpstreamError, safe_request_id
from tests.conftest import login, seed
from tests.test_management import wait_job


async def test_settings_persist_resize_and_retention(installation):
    app, client, settings = installation
    await login(client)
    assert (await client.get("/api/settings")).json()["schema"] == "0006"
    assert (
        await client.put(
            "/api/settings", json={"request_timeout": 0, "concurrency": 99, "log_retention_days": -1}
        )
    ).status_code == 422
    with app.state.db.begin() as db:
        for status in ["success", "failed", "uncertain"]:
            job = Job(kind="invite", status=status, finished_at=now() - 3 * 86400)
            db.add(job)
            db.flush()
            db.add(JobItem(job_id=job.id, action="invite", target="a@example.com", status=status))
        db.add(Audit(created_at=now() - 3 * 86400, action="test", status="success"))
    response = await client.put(
        "/api/settings", json={"request_timeout": 12.5, "concurrency": 2, "log_retention_days": 1}
    )
    assert response.status_code == 200
    assert app.state.services.gateway.timeout == 12.5
    assert app.state.services.semaphore.limit == 2
    with app.state.db() as db:
        assert list(db.scalars(select(Job.status))) == ["uncertain"]
        assert list(db.scalars(select(JobItem.status))) == ["uncertain"]
        assert not db.scalar(select(Audit.id).where(Audit.action == "test"))
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.services.gateway.timeout == 12.5
        assert restarted.state.services.semaphore.limit == 2
        with restarted.state.db() as db:
            assert db.get(SystemSettings, 1).log_retention_days == 1


async def test_gate_resize_keeps_running_and_waiting_requests():
    gate = ConcurrencyGate(2)
    await gate.__aenter__()
    await gate.__aenter__()
    await gate.resize(1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiter():
        async with gate:
            entered.set()
            await release.wait()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not entered.is_set()
    await gate.__aexit__()
    await asyncio.sleep(0)
    assert not entered.is_set()
    await gate.resize(2)
    await asyncio.wait_for(entered.wait(), 1)
    assert gate.active == 2
    release.set()
    await task
    await gate.__aexit__()
    assert gate.active == 0


async def test_node_history_probe_dedup_and_correlated_audit(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    node, space = await seed(client)
    services = app.state.services
    entered, release = asyncio.Event(), asyncio.Event()

    async def test_node(n):
        entered.set()
        await release.wait()
        return "203.0.113.7"

    async def upstream(w, n, method, resource, params=None, body=None):
        if resource == "invites":
            raise UpstreamError("permission_denied", request_id="req_invite")
        return GatewayResult({"data": [], "has_more": False, "last_id": None}, "req_users")

    monkeypatch.setattr(services.gateway, "test_node", test_node)
    monkeypatch.setattr(services.gateway, "workspace", upstream)
    job_id = services.schedule_probe(space["id"])
    await asyncio.wait_for(entered.wait(), 1)
    assert services.schedule_probe(space["id"]) is None
    assert (await client.post("/api/probe-all")).json()["scheduled"] == 0
    assert (await client.post(f"/api/workspaces/{space['id']}/probe")).status_code == 409
    release.set()
    job = await wait_job(client, job_id)
    assert job["status"] == "failed"
    assert job["kind"] == "probe" and job["workspace_name"] == space["name"]
    assert job["started_at"] and job["finished_at"]
    nodes = (await client.get("/api/nodes")).json()
    assert nodes[0]["last_test_ip"] == "203.0.113.7"
    assert nodes[0]["workspaces"] == [{"id": space["id"], "name": space["name"]}]
    history = (await client.get(f"/api/nodes/{node['id']}/tests")).json()
    assert history[0]["status"] == "success"
    records = (await client.get(f"/api/audit?workspace_id={space['id']}")).json()
    event = next(a for a in records if a["action"] == "probe_invites")
    assert event["error_code"] == "permission_denied"
    assert event["job_id"] == job_id and event["request_id"] == "req_invite"
    assert all(a["workspace_id"] == space["id"] for a in records)
    assert (await client.get("/api/audit?workspace_id=999999")).json() == []
    older = (await client.get(f"/api/audit?workspace_id={space['id']}&before={event['id']}")).json()
    assert all(a["id"] < event["id"] for a in older)

    async def failure(n):
        raise UpstreamError("proxy_auth")

    monkeypatch.setattr(services.gateway, "test_node", failure)
    assert (await client.post(f"/api/nodes/{node['id']}/test")).status_code == 502
    with app.state.db() as db:
        assert db.get(Node, node["id"]).last_test_status == "proxy_auth"
        assert len(list(db.scalars(select(NodeTest)))) == 2


async def test_probe_restart_is_failed_not_uncertain(installation):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    job_id = app.state.services.prepare_probe(space["id"])
    with app.state.db.begin() as db:
        db.get(Job, job_id).status = "running"
        db.scalar(select(JobItem).where(JobItem.job_id == job_id)).status = "running"
    app.state.services.recover()
    with app.state.db() as db:
        assert db.get(Job, job_id).status != "uncertain"
        assert db.scalar(select(JobItem.status).where(JobItem.job_id == job_id)) == "failed"


def test_request_id_accepts_only_trace_identifiers():
    assert safe_request_id("req_abc-123") == "req_abc-123"
    assert safe_request_id("12345678-1234-1234-1234-123456789abc")
    for value in ["Bearer secret", "key@example.com", "req_a\nsecret", "req_" + "a" * 121, None]:
        assert safe_request_id(value) == ""


def test_upgrade_0002_preserves_populated_foreign_keys(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url)
    config = Config("alembic.ini")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        config.attributes["connection"] = conn
        command.upgrade(config, "0002")
        statements = [
            "INSERT INTO proxy_nodes VALUES (1,'node','http','localhost',8080,'enc-user','enc-pass',1,'')",
            "INSERT INTO workspaces VALUES (1,'space','ws','enc-key',1,1,0,'',NULL,'success')",
            "INSERT INTO jobs VALUES (1,1,'invite','uncertain',1,2)",
            "INSERT INTO job_items VALUES (1,1,'member@example.com','invite','uncertain','{}')",
            "INSERT INTO probe_runs VALUES (1,1,'node',1,'success','{}',10)",
            "INSERT INTO audit_events VALUES (1,1,'space','node','invite','member@example.com','uncertain')",
        ]
        for statement in statements:
            conn.execute(text(statement))
    engine.dispose()
    upgraded, factory = database(url)
    with factory() as db:
        for table in ["proxy_nodes", "workspaces", "jobs", "job_items", "probe_runs", "audit_events"]:
            assert db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == 1
        assert db.get(Job, 1).workspace_name == "space"
        assert db.get(Node, 1).password_enc == "enc-pass"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    upgraded.dispose()
