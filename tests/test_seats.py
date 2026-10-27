import asyncio

import pytest
from sqlalchemy import select, func

from backend.models import Workspace, Node, Redemption, SeatReservation, now
from backend.upstream import GatewayResult, UpstreamError
from tests.conftest import login, seed
from tests.test_management import wait_job

HEADERS = {"X-TeamManager": "1"}


def snapshot(app, wid, total=5, members=1, invites=0):
    with app.state.db.begin() as db:
        w = db.get(Workspace, wid)
        w.seat_limit, w.member_count, w.invite_count = total, members, invites
        w.seat_status, w.seat_checked_at = "success", now()


async def generate(client, count=1):
    r = await client.post("/api/cdks", json={"count": count})
    assert r.status_code == 200
    return r.json()["codes"]


async def redeem(client, code, email):
    return await client.post("/api/public/redeem", json={"code": code, "email": email}, headers=HEADERS)


async def setup(installation):
    app, client, _ = installation
    await login(client)
    node, space = await seed(client)
    return app, client, node, space


async def test_capacity_field_validation_and_persistence(installation):
    app, client, node, space = await setup(installation)
    body = {
        "name": "Capacity",
        "workspace_id": "capacity",
        "node_id": node["id"],
        "admin_key": "test-key",
        "seat_limit": 8,
    }
    created = await client.post("/api/workspaces", json=body)
    assert created.status_code == 200 and created.json()["seat_limit"] == 8
    assert created.json()["seats"]["available"] == 0
    assert (await client.post("/api/workspaces", json=body | {"seat_limit": -1})).status_code == 422
    body["seat_limit"] = 9
    updated = await client.put(f"/api/workspaces/{created.json()['id']}", json=body)
    assert updated.json()["seat_limit"] == 9


async def test_universal_code_order_and_reservation(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    second = (
        await client.post(
            "/api/workspaces",
            json={"name": "A", "workspace_id": "second", "node_id": node["id"], "admin_key": "key"},
        )
    ).json()
    snapshot(app, space["id"])
    snapshot(app, second["id"])
    monkeypatch.setattr(app.state.services, "spawn", lambda c: c.close())
    code = (await generate(client))[0]
    r = await redeem(client, code, "new@example.com")
    assert r.status_code == 200
    with app.state.db() as db:
        assert db.scalar(select(Redemption)).workspace_id == second["id"]
        assert db.scalar(select(SeatReservation)).workspace_id == second["id"]
    assert (await client.get("/api/cdks")).json()[0]["workspace_id"] == second["id"]
    summary = (await client.get("/api/public/availability")).json()
    assert summary["available"] == 7
    assert "A" not in summary and "email" not in summary


async def test_last_seat_concurrent_codes_and_duplicate_email(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=2)
    monkeypatch.setattr(app.state.services, "spawn", lambda c: c.close())
    codes = await generate(client, 3)
    results = await asyncio.gather(
        redeem(client, codes[0], "a@example.com"), redeem(client, codes[1], "b@example.com")
    )
    assert sorted(r.status_code for r in results) == [200, 503]
    winner = "a@example.com" if results[0].status_code == 200 else "b@example.com"
    assert (await redeem(client, codes[2], winner)).status_code == 409
    with app.state.db() as db:
        assert db.scalar(select(func.count()).select_from(Redemption)) == 1


@pytest.mark.parametrize("kind", ["disabled", "node", "stale", "zero"])
async def test_unavailable_capacity_does_not_consume_code(installation, monkeypatch, kind):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"])
    with app.state.db.begin() as db:
        w = db.get(Workspace, space["id"])
        if kind == "disabled":
            w.enabled = False
        if kind == "node":
            db.get(Node, node["id"]).health_status = "unhealthy"
        if kind == "stale":
            w.seat_checked_at = now() - 600
        if kind == "zero":
            w.seat_limit = 0

    async def fail(*args, **kwargs):
        raise UpstreamError("permission_denied")

    monkeypatch.setattr(app.state.services.gateway, "_workspace", fail)
    assert (await redeem(client, (await generate(client))[0], "new@example.com")).status_code == 503
    with app.state.db() as db:
        assert db.scalar(select(func.count()).select_from(Redemption)) == 0


async def test_snapshot_pagination_dedup_and_reflection(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=6, members=0)
    services = app.state.services
    monkeypatch.setattr(services, "spawn", lambda c: c.close())
    assert (await redeem(client, (await generate(client))[0], "held@example.com")).status_code == 200
    calls = []

    async def page(w, n, method, resource, params=None, body=None):
        calls.append((resource, params.get("after")))
        rows = [{"id": "m1", "email": "owner@example.com"}]
        more = resource == "users" and not params.get("after")
        if resource == "invites":
            rows = [
                {"email": "held@example.com", "status": "pending"},
                {"email": "owner@example.com"},
                {"email": "old@example.com", "status": "expired"},
            ]
        return GatewayResult({"data": rows, "has_more": more, "last_id": "next" if more else None})

    monkeypatch.setattr(services.gateway, "_workspace", page)
    r = await client.post(f"/api/workspaces/{space['id']}/refresh-seats")
    assert r.status_code == 200, r.text
    assert r.json()["members"] == 1 and r.json()["invites"] == 1
    assert r.json()["reserved"] == 0 and r.json()["available"] == 4
    assert len(calls) == 3
    with app.state.db() as db:
        assert db.scalar(select(SeatReservation)).state == "reflected"
        assert "held@example.com" not in db.get(Workspace, space["id"]).seat_emails_json
    calls.clear()
    assert (await client.get("/api/public/availability")).json()["available"] == 4
    assert not calls


@pytest.mark.parametrize("uncertain", [True, False])
async def test_settlement_and_admin_release(installation, monkeypatch, uncertain):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=2)

    async def fail(*args, **kwargs):
        if args[2] == "GET":
            return GatewayResult({"data": [], "has_more": False})
        raise UpstreamError("timeout", uncertain=uncertain)

    monkeypatch.setattr(app.state.services.gateway, "_workspace", fail)
    r = await redeem(client, (await generate(client))[0], "new@example.com")
    assert r.status_code == 200
    with app.state.db() as db:
        jid = db.scalar(select(Redemption)).job_id
    await wait_job(client, jid)
    with app.state.db() as db:
        reservation = db.scalar(select(SeatReservation))
        assert reservation.state == ("uncertain" if uncertain else "released")
        rid = reservation.id
    if uncertain:
        assert (
            await client.post(f"/api/reservations/{rid}/release", json={"confirm": str(rid)})
        ).status_code == 200
        app.state.services.recover()
        with app.state.db() as db:
            assert db.get(SeatReservation, rid).state == "released"


async def test_manual_invite_shares_capacity_and_running_release_rejected(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], total=2)
    monkeypatch.setattr(app.state.services, "spawn", lambda c: c.close())
    r = await client.post(f"/api/workspaces/{space['id']}/invite", json={"emails": ["manual@example.com"]})
    assert r.status_code == 200, r.text
    with app.state.db() as db:
        rid = db.scalar(select(SeatReservation.id))
    assert (
        await client.post(f"/api/reservations/{rid}/release", json={"confirm": str(rid)})
    ).status_code == 409
    assert (await redeem(client, (await generate(client))[0], "public@example.com")).status_code == 503


async def test_failed_snapshot_is_atomic_and_backs_off(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    snapshot(app, space["id"], members=2)
    calls = []

    async def page(w, n, method, resource, params=None, body=None):
        calls.append(resource)
        if resource == "invites":
            raise UpstreamError("permission_denied")
        return GatewayResult({"data": [], "has_more": False})

    services = app.state.services
    monkeypatch.setattr(services.gateway, "_workspace", page)
    assert (await client.post(f"/api/workspaces/{space['id']}/refresh-seats")).status_code != 200
    with app.state.db() as db:
        w = db.get(Workspace, space["id"])
        assert w.member_count == 2 and services.seats.info(db, w)["available"] == 0
    await services.seats.ensure_pool()
    assert calls == ["users", "invites"]


async def test_invitation_jobs_are_global_serial_across_workspaces(installation, monkeypatch):
    app, client, node, space = await setup(installation)
    second = (
        await client.post(
            "/api/workspaces",
            json={"name": "B", "workspace_id": "serial-b", "node_id": node["id"], "admin_key": "key"},
        )
    ).json()
    running, peak, calls = 0, 0, []

    async def invite(w, n, method, resource, params=None, body=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        calls.append(w.id)
        await asyncio.sleep(0.03)
        running -= 1
        return GatewayResult({"id": "invite"})

    monkeypatch.setattr(app.state.services.gateway, "_workspace", invite)
    first, last = await asyncio.gather(
        client.post(f"/api/workspaces/{space['id']}/invite", json={"emails": ["one@example.com"]}),
        client.post(f"/api/workspaces/{second['id']}/invite", json={"emails": ["two@example.com"]}),
    )
    assert first.status_code == last.status_code == 200
    await wait_job(client, first.json()["job_id"])
    await wait_job(client, last.json()["job_id"])
    assert peak == 1 and set(calls) == {space["id"], second["id"]}
