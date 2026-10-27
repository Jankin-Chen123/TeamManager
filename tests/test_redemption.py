import asyncio

from sqlalchemy import func, select

from backend.models import CDK, Redemption, Job, JobItem, Node, Workspace, now
from backend.security import token_hash
from backend.upstream import GatewayResult, UpstreamError
from tests.conftest import login, seed as base_seed
from tests.test_management import wait_job

HEADERS = {"X-TeamManager": "1"}


async def seed(client):
    node, space = await base_seed(client)
    app = client._transport.app
    with app.state.db.begin() as db:
        workspace = db.get(Workspace, space["id"])
        workspace.seat_limit = 10
        workspace.seat_status, workspace.seat_checked_at = "success", now()
    return node, space


async def codes(client, space, **kwargs):
    response = await client.post("/api/cdks", json={"workspace_id": space["id"], **kwargs})
    assert response.status_code == 200
    return response.json()["codes"]


async def test_concurrent_redemption_is_single_job_and_single_invite(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    code = (await codes(client, space))[0]
    calls = []

    async def invite(workspace, node, method, resource, params=None, body=None):
        if method == "GET":
            return GatewayResult({"data": [], "has_more": False})
        calls.append((method, resource))
        await asyncio.sleep(0)
        return GatewayResult({"id": "invite", "email": body["email"]})

    monkeypatch.setattr(app.state.services.gateway, "_workspace", invite)
    responses = await asyncio.gather(
        *[
            client.post("/api/public/redeem", json={"code": code, "email": "a@example.com"}, headers=HEADERS)
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in responses)
    receipts = {r.json()["receipt"] for r in responses}
    assert len(receipts) == 1
    with app.state.db() as db:
        assert db.scalar(select(func.count()).select_from(Redemption)) == 1
        redemption = db.scalar(select(Redemption))
        job_id = redemption.job_id
        assert redemption.receipt_enc != next(iter(receipts))
        assert db.scalar(select(CDK)).code_hash == token_hash(code)
    job = await wait_job(client, job_id)
    assert job["status"] == "success" and calls == [("POST", "invites")]
    result = await client.post("/api/public/redemption-status", json={"receipt": next(iter(receipts))})
    assert result.json()["status"] == "success" and "email" not in result.json()
    denied = await client.post(
        "/api/public/redeem", json={"code": code, "email": "other@example.com"}, headers=HEADERS
    )
    assert denied.status_code == 400
    listed = await client.get("/api/cdks")
    assert listed.json()[0]["status"] == "success"
    assert code not in listed.text and next(iter(receipts)) not in listed.text
    assert code not in (await client.get("/api/audit")).text


async def test_expiry_binding_revocation_and_broken_node_do_not_consume(installation):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    code = (await codes(client, space, bound_email="bound@example.com"))[0]
    result = await client.post(
        "/api/public/redeem", json={"code": code, "email": "other@example.com"}, headers=HEADERS
    )
    assert result.status_code == 400
    with app.state.db.begin() as db:
        db.get(Node, 1).blocked_until = now() + 600
    result = await client.post(
        "/api/public/redeem", json={"code": code, "email": "bound@example.com"}, headers=HEADERS
    )
    assert result.status_code == 503
    with app.state.db.begin() as db:
        assert db.scalar(select(func.count()).select_from(Redemption)) == 0
        db.get(Node, 1).blocked_until = 0
        db.get(CDK, 1).expires_at = now() - 1
    result = await client.post(
        "/api/public/redeem", json={"code": code, "email": "bound@example.com"}, headers=HEADERS
    )
    assert result.status_code == 400
    code = (await codes(client, space))[0]
    result = await client.post("/api/cdks/2/revoke", json={"confirm": "2"})
    assert result.status_code == 200
    result = await client.post(
        "/api/public/redeem", json={"code": code, "email": "a@example.com"}, headers=HEADERS
    )
    assert result.status_code == 400
    assert (
        await client.post("/api/cdks", json={"workspace_id": 1, "expires_at": now() - 1})
    ).status_code == 422


async def test_uncertain_invite_never_replayed_on_repeat(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    code = (await codes(client, space))[0]
    calls = []

    async def timeout(*args, **kwargs):
        if args[2] == "GET":
            return GatewayResult({"data": [], "has_more": False})
        calls.append(1)
        raise UpstreamError("timeout", uncertain=True)

    monkeypatch.setattr(app.state.services.gateway, "_workspace", timeout)
    first = await client.post(
        "/api/public/redeem", json={"code": code, "email": "a@example.com"}, headers=HEADERS
    )
    with app.state.db() as db:
        job_id = db.scalar(select(Redemption)).job_id
    assert (await wait_job(client, job_id))["status"] == "uncertain"
    repeated = await client.post(
        "/api/public/redeem", json={"code": code, "email": "a@example.com"}, headers=HEADERS
    )
    assert repeated.json()["receipt"] == first.json()["receipt"] and repeated.json()["status"] == "uncertain"
    assert len(calls) == 1
    assert (await client.post("/api/cdks/1/revoke", json={"confirm": "1"})).status_code == 409


async def test_recovery_resumes_only_unstarted_public_jobs(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    code = (await codes(client, space))[0]
    services = app.state.services
    original_spawn = services.spawn
    monkeypatch.setattr(services, "spawn", lambda coroutine: coroutine.close())
    await client.post("/api/public/redeem", json={"code": code, "email": "a@example.com"}, headers=HEADERS)
    with app.state.db() as db:
        job_id = db.scalar(select(Redemption)).job_id
        assert db.get(Job, job_id).status == "queued"
    calls = []

    async def invite(*args, **kwargs):
        if args[2] == "GET":
            return GatewayResult({"data": [], "has_more": False})
        calls.append(1)
        return GatewayResult({"id": "invite"})

    monkeypatch.setattr(services.gateway, "_workspace", invite)
    monkeypatch.setattr(services, "spawn", original_spawn)
    services.recover()
    await asyncio.gather(services.run_job(job_id), services.run_job(job_id))
    assert (await wait_job(client, job_id))["status"] == "success" and len(calls) == 1
    with app.state.db.begin() as db:
        db.get(Job, job_id).status = "running"
        db.scalar(select(JobItem).where(JobItem.job_id == job_id)).status = "running"
    services.recover()
    assert (await wait_job(client, job_id))["status"] == "uncertain" and len(calls) == 1


async def test_public_limits_and_admin_isolation(installation):
    _, client, _ = installation
    assert (await client.get("/api/cdks")).status_code == 401
    assert (await client.post("/api/cdks", json={"workspace_id": 1})).status_code == 401
    for _ in range(20):
        result = await client.post(
            "/api/public/redeem", json={"code": "TM-" + "A" * 40, "email": "a@example.com"}, headers=HEADERS
        )
        assert result.status_code == 400
    assert (
        await client.post("/api/public/redemption-status", json={"receipt": "invalid" * 5})
    ).status_code == 429
    assert (
        await client.post("/api/public/redeem", json={"code": "TM-" + "A" * 40, "email": "a@example.com"})
    ).status_code == 403
