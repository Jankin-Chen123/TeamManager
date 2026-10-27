import asyncio
import json

import pytest
from sqlalchemy import select

from backend.models import Job, JobItem, Node, Redemption, SeatReservation, Workspace, now
from backend.security import token_hash
from backend.upstream import GatewayResult, UpstreamError
from backend.web_session import normalize_page
from tests.test_seats import setup, snapshot, generate, redeem


async def queue(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=2)
    original_spawn = app.state.services.spawn
    monkeypatch.setattr(app.state.services, "spawn", lambda c: c.close())
    code = (await generate(client))[0]
    assert (await redeem(client, code, "target@example.com")).status_code == 200
    with app.state.db() as db:
        jid = db.scalar(select(Redemption.job_id))
    return app, client, space, jid, code, original_spawn


@pytest.mark.parametrize("failure", ["none", "invites", "node"])
async def test_combined_sync_collects_pages_once_and_updates_capacity(installation, monkeypatch, failure):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=6, members=1)
    calls = []

    async def test_node(n):
        if failure == "node":
            raise UpstreamError("unavailable")
        return "203.0.113.1"

    async def request(w, n, method, resource, params=None, body=None):
        calls.append((method, resource, dict(params)))
        if resource == "invites":
            if failure == "invites":
                raise UpstreamError("permission_denied")
            return GatewayResult(
                {"data": [{"id": "i", "email": "pending@example.com"}], "has_more": False}, "req_invites"
            )
        more = not params.get("after")
        return GatewayResult(
            {
                "data": [{"id": "a" if more else "b", "email": "a@example.com" if more else "b@example.com"}],
                "has_more": more,
                "last_id": "a" if more else None,
            },
            "req_members",
        )

    monkeypatch.setattr(app.state.services.gateway, "test_node", test_node)
    monkeypatch.setattr(app.state.services.gateway, "workspace", request)
    response = await client.post(f"/api/workspaces/{space['id']}/probe")
    assert response.status_code == 200
    assert response.json()["status"] == ("success" if failure == "none" else "partial")
    assert len(calls) == 3
    assert all(method == "GET" and params["limit"] == 100 for method, _, params in calls)
    current = (await client.get("/api/workspaces")).json()[0]
    if failure == "invites":
        assert current["seats"]["status"] == "permission_denied"
        assert current["seats"]["available"] == 0
        assert current["seats"]["members"] == 1
    else:
        assert current["seats"]["members"] == 2
        assert current["seats"]["invites"] == 1
        assert response.json()["details"]["users"]["request_id"] == "req_members"
        if failure == "none":
            assert current["seats"]["available"] == 3


@pytest.mark.parametrize("failure", ["full", "existing", "read_error"])
async def test_preflight_never_writes_when_not_verified(installation, monkeypatch, failure):
    app, client, space, jid, code, _ = await queue(installation, monkeypatch)
    writes = []

    async def gateway(w, n, method, resource, params=None, body=None):
        if method != "GET":
            writes.append(1)
            return GatewayResult({"id": "unexpected"})
        if failure == "read_error":
            raise UpstreamError("timeout", uncertain=True)
        rows = []
        if resource == "users":
            rows = (
                [{"email": "target@example.com"}]
                if failure == "existing"
                else [{"email": "one@example.com"}, {"email": "two@example.com"}]
            )
        return GatewayResult({"data": rows, "has_more": False})

    monkeypatch.setattr(app.state.services.gateway, "_workspace", gateway)
    await app.state.services.run_job(jid)
    assert not writes
    with app.state.db() as db:
        assert db.get(Job, jid).status == "failed"
        assert db.scalar(select(JobItem)).status == "failed"
        assert not json.loads(db.scalar(select(JobItem)).result_json).get("uncertain")


async def test_stale_snapshot_does_not_reject_email(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"])
    with app.state.db.begin() as db:
        old = Workspace(
            name="old",
            workspace_id="old-id",
            node_id=node["id"],
            key_enc=app.state.vault.encrypt("key"),
            seat_limit=0,
            seat_status="success",
            seat_checked_at=now() - 600,
            seat_emails_json=json.dumps([token_hash("target@example.com")]),
        )
        db.add(old)
    monkeypatch.setattr(app.state.services, "spawn", lambda c: c.close())
    assert (await redeem(client, (await generate(client))[0], "target@example.com")).status_code == 200


async def test_compensation_reuses_receipt_and_cannot_duplicate_or_retry_unknown(installation, monkeypatch):
    app, client, space, jid, code, _ = await queue(installation, monkeypatch)

    async def gateway(w, n, method, resource, params=None, body=None):
        if method == "GET":
            return GatewayResult({"data": [], "has_more": False})
        raise UpstreamError("request_rejected")

    monkeypatch.setattr(app.state.services.gateway, "_workspace", gateway)
    services = app.state.services
    await services.run_job(jid)
    original = await redeem(client, code, "target@example.com")
    r = await client.post("/api/cdks/1/retry", json={"confirm": "1"})
    assert r.status_code == 200, r.text
    new_id = r.json()["job_id"]
    assert new_id != jid
    repeated = await redeem(client, code, "target@example.com")
    assert repeated.json()["receipt"] == original.json()["receipt"]
    assert repeated.json()["status"] == "queued"
    assert (await client.post("/api/cdks/1/retry", json={"confirm": "1"})).status_code == 409

    async def unknown(w, n, method, resource, params=None, body=None):
        if method == "GET":
            return GatewayResult({"data": [], "has_more": False})
        raise UpstreamError("timeout", uncertain=True)

    monkeypatch.setattr(services.gateway, "_workspace", unknown)
    await services.run_job(new_id)
    assert (await client.post("/api/cdks/1/retry", json={"confirm": "1"})).status_code == 409
    records = (await client.get(f"/api/workspaces/{space['id']}/reservations")).json()
    assert records[0]["email"] == "target@example.com"
    rid = records[0]["id"]
    assert (
        await client.post(f"/api/reservations/{rid}/release", json={"confirm": str(rid)})
    ).status_code == 200
    with app.state.db.begin() as db:
        node = db.get(Node, 1)
        node.health_status, node.blocked_until = "healthy", 0
    assert (await client.post("/api/cdks/1/retry", json={"confirm": "1"})).status_code == 200


async def test_cancel_queue_releases_capacity_and_never_runs(installation, monkeypatch):
    app, client, space, jid, code, _ = await queue(installation, monkeypatch)
    listed = (await client.get("/api/jobs")).json()
    assert listed[0]["queue_position"] == 1
    assert (await client.post(f"/api/jobs/{jid}/cancel", json={"confirm": str(jid)})).status_code == 200
    await app.state.services.run_job(jid)
    with app.state.db() as db:
        assert db.get(Job, jid).status == "cancelled"
        assert db.scalar(select(SeatReservation)).state == "released"
    assert (await client.post(f"/api/jobs/{jid}/cancel", json={"confirm": str(jid)})).status_code == 409


async def test_queue_prevents_identity_change(installation, monkeypatch):
    app, client, space, jid, code, _ = await queue(installation, monkeypatch)
    r = await client.put(
        f"/api/workspaces/{space['id']}",
        json={
            "name": "edited",
            "workspace_id": "different",
            "node_id": 1,
            "admin_key": "new-key",
            "seat_limit": 2,
        },
    )
    assert r.status_code == 409
    with app.state.db() as db:
        assert db.get(Workspace, space["id"]).workspace_id == space["workspace_id"]


async def test_maintenance_health_and_session_alerts(installation):
    app, client, _ = installation
    assert (await client.get("/api/maintenance")).status_code == 401
    from tests.conftest import login, seed

    await login(client)
    _, space = await seed(client)
    with app.state.db.begin() as db:
        w = db.get(Workspace, space["id"])
        w.credential_type = "web_session"
        w.auto_refresh = True
        w.refresh_status = "web_verification"
    r = await client.get("/api/maintenance")
    assert r.status_code == 200
    assert r.json()["alerts"][0]["code"] == "session_cookie_missing"
    assert "key_enc" not in r.text
    app.state.services.maintenance["last_tick_at"] = now() - 100
    assert (await client.get("/api/health")).status_code == 503
    app.state.services.maintenance["last_tick_at"] = now()


async def test_scheduler_continues_after_failure(installation, monkeypatch):
    app, _, _ = installation
    services = app.state.services
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic-error")
        raise asyncio.CancelledError

    async def sleep(_):
        pass

    monkeypatch.setattr(services, "maintenance_tick", tick)
    monkeypatch.setattr("backend.services.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await services.scheduler()
    assert len(calls) == 2 and services.maintenance["failure_count"] == 1


def test_invite_pagination_does_not_drop_full_page():
    raw = {"items": [{"email": f"person{i}@example.com"} for i in range(100)]}
    assert normalize_page(raw, "invites", 0, 100)["has_more"]
    assert not normalize_page({"items": []}, "invites", 100, 100)["has_more"]


async def test_confirm_uncertain_invite_requires_observed_email(installation, monkeypatch):
    app, client, space, jid, code, _ = await queue(installation, monkeypatch)
    services = app.state.services

    async def gateway(w, n, method, resource, params=None, body=None):
        if method == "GET":
            return GatewayResult({"data": [], "has_more": False})
        raise UpstreamError("timeout", uncertain=True)

    monkeypatch.setattr(services.gateway, "_workspace", gateway)
    await services.run_job(jid)
    with app.state.db.begin() as db:
        n = db.get(Node, 1)
        n.health_status = "healthy"
        n.blocked_until = 0
    r = await client.post(f"/api/jobs/{jid}/confirm-invitation", json={"confirm": str(jid)})
    assert r.status_code == 409
    calls = []

    async def observed(w, n, method, resource, params=None, body=None):
        calls.append(method)
        return GatewayResult(
            {"data": [{"email": "target@example.com"}] if resource == "invites" else [], "has_more": False}
        )

    monkeypatch.setattr(services.gateway, "_workspace", observed)
    assert (
        await client.post(f"/api/jobs/{jid}/confirm-invitation", json={"confirm": str(jid)})
    ).status_code == 200
    assert calls == ["GET", "GET"]
    with app.state.db() as db:
        assert db.get(Job, jid).status == "success"
        assert db.scalar(select(SeatReservation)).state == "reflected"


async def test_recovery_resumes_manual_unstarted_invitations(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    services = app.state.services
    monkeypatch.setattr(services, "spawn", lambda c: c.close())
    jid = services.create_job(space["id"], "invite", ["manual@example.com"])
    services.recover()
    with app.state.db() as db:
        assert db.get(Job, jid).status == "queued"
        assert db.scalar(select(JobItem)).status == "queued"


async def test_repeated_full_page_fails_closed(installation, monkeypatch):
    app, client, node, space = await setup(installation)

    async def repeated(*args, **kwargs):
        return GatewayResult(
            {
                "data": [{"id": str(i), "email": f"p{i}@example.com"} for i in range(100)],
                "has_more": True,
                "last_id": str(int((kwargs.get("params") or {}).get("after", "0")) + 100),
            }
        )

    monkeypatch.setattr(app.state.services.gateway, "_workspace", repeated)
    with pytest.raises(UpstreamError) as exc:
        await app.state.services.seats.refresh(space["id"])
    assert exc.value.code == "invalid_response"
