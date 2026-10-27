import asyncio

import pytest
from fastapi import HTTPException

from backend.models import Node, Workspace, now
from backend.upstream import GatewayResult, UpstreamError
from backend.web_session import WebSessionGateway
from tests.conftest import login, seed
from tests.test_web_session import session_token


async def web_space(app, client, expiry=None, cookie="renewal-secret"):
    await login(client)
    node, space = await seed(client)
    with app.state.db.begin() as db:
        w = db.get(Workspace, space["id"])
        w.credential_type = "web_session"
        w.key_enc = app.state.vault.encrypt(session_token(expiry=expiry))
        w.token_expires_at = expiry or now() + 100
        w.session_token_enc = app.state.vault.encrypt(cookie)
    return node, space


async def test_passive_node_detection_cooldown_and_recovery(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    node, space = await seed(client)
    services = app.state.services
    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        raise UpstreamError("proxy_connection")

    monkeypatch.setattr(services.gateway, "request", request)
    response = await client.get(f"/api/workspaces/{space['id']}/users")
    assert response.json()["detail"]["code"] == "proxy_connection"
    response = await client.get(f"/api/workspaces/{space['id']}/users")
    assert response.json()["detail"]["code"] == "node_cooling" and len(calls) == 1
    with app.state.db() as db:
        n = db.get(Node, node["id"])
        assert n.health_status == "unhealthy" and n.blocked_until > now()

    async def ip(*args):
        return "203.0.113.1"

    monkeypatch.setattr(services.gateway, "test_node", ip)
    await services.automation.recover_node(node["id"])
    with app.state.db() as db:
        n = db.get(Node, node["id"])
        assert n.health_status == "healthy" and n.blocked_until == 0 and n.failure_count == 0

    async def denied(*args, **kwargs):
        raise UpstreamError("permission_denied")

    monkeypatch.setattr(services.gateway, "request", denied)
    await client.get(f"/api/workspaces/{space['id']}/users")
    with app.state.db() as db:
        assert db.get(Node, node["id"]).health_status == "healthy"


async def test_timeout_is_suspect_and_old_binding_cannot_poison_new_node(installation):
    app, client, _ = installation
    await login(client)
    node, space = await seed(client)
    automation = app.state.services.automation
    _, old = app.state.services.pair(space["id"])
    automation.report_node(old, "timeout")
    with app.state.db.begin() as db:
        live = db.get(Node, node["id"])
        assert live.health_status == "suspect"
        live.port = 9999
        live.health_status = "unknown"
    automation.report_node(old, "proxy_connection")
    with app.state.db() as db:
        assert db.get(Node, node["id"]).health_status == "unknown"


async def test_refresh_happens_before_single_write_and_stores_rotated_cookie(installation, monkeypatch):
    app, client, _ = installation
    _, space = await web_space(app, client, now() - 1)
    services = app.state.services
    fresh = session_token(expiry=now() + 7200)
    calls = []

    async def refresh(self, node, method, path, **kwargs):
        assert kwargs["cookie"] == "renewal-secret" and not kwargs.get("token")
        calls.append("refresh")
        return GatewayResult(
            {"accessToken": fresh, "sessionToken": "rotated-secret", "account": {"id": "workspace-a"}}
        )

    async def write(workspace, node, method, resource, params=None, body=None):
        assert app.state.vault.decrypt(workspace.key_enc) == fresh
        calls.append(method)
        return GatewayResult({"id": "invited", "email": body["email"]})

    monkeypatch.setattr(WebSessionGateway, "request", refresh)
    monkeypatch.setattr(services.gateway, "_workspace", write)
    async with services.lock(space["id"]):
        w, n = services.pair(space["id"])
        await services.gateway.workspace(w, n, "POST", "invites", body={"email": "test@example.com"})
    assert calls == ["refresh", "POST"]
    with app.state.db() as db:
        w = db.get(Workspace, space["id"])
        assert w.refresh_status == "success" and w.refresh_next_at > now()
        assert app.state.vault.decrypt(w.session_token_enc) == "rotated-secret"
    response = await client.get("/api/workspaces")
    assert fresh not in response.text and "rotated-secret" not in response.text


async def test_failed_refresh_backs_off_and_preserves_credentials(installation, monkeypatch):
    app, client, _ = installation
    _, space = await web_space(app, client)
    calls = []

    async def rejected(*args, **kwargs):
        calls.append(1)
        raise UpstreamError("session_expired")

    monkeypatch.setattr(WebSessionGateway, "request", rejected)
    service = app.state.services
    before = service.pair(space["id"])[0].key_enc
    await service.automation.refresh_one(space["id"])
    await service.automation.refresh_one(space["id"])
    assert len(calls) == 1
    with app.state.db() as db:
        w = db.get(Workspace, space["id"])
        assert w.key_enc == before and w.refresh_status == "session_expired"
        assert w.refresh_next_at > now() + 21000
        assert db.get(Node, 1).health_status == "healthy"


async def test_background_refresh_dedup_and_opt_out(installation, monkeypatch):
    app, client, _ = installation
    _, space = await web_space(app, client)
    service = app.state.services
    calls = []

    async def refresh(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(0)
        return GatewayResult(
            {"accessToken": session_token(expiry=now() + 7200), "account": {"id": "workspace-a"}}
        )

    monkeypatch.setattr(WebSessionGateway, "request", refresh)
    service.automation.tick()
    service.automation.tick()
    await asyncio.gather(*list(service.tasks))
    assert len(calls) == 1
    with app.state.db.begin() as db:
        w = db.get(Workspace, space["id"])
        w.auto_refresh = False
        w.token_expires_at = now() - 1
        w.refresh_next_at = 0
    service.automation.tick()
    assert not service.automation.pending_refresh


async def test_refresh_rejects_concurrent_configuration_change(installation, monkeypatch):
    app, client, _ = installation
    _, space = await web_space(app, client)
    service = app.state.services
    old, node = service.pair(space["id"])

    async def refresh(*args, **kwargs):
        with app.state.db.begin() as db:
            db.get(Workspace, space["id"]).key_enc = app.state.vault.encrypt("new-configuration")
        return GatewayResult({"accessToken": session_token(expiry=now() + 7200)})

    monkeypatch.setattr(WebSessionGateway, "request", refresh)
    with pytest.raises(HTTPException) as result:
        await service.automation.refresh(old, node)
    assert result.value.status_code == 409
    with app.state.db() as db:
        assert app.state.vault.decrypt(db.get(Workspace, space["id"]).key_enc) == "new-configuration"


async def test_missing_cookie_suspends_automatic_attempts(installation):
    app, client, _ = installation
    _, space = await web_space(app, client, cookie="")
    await app.state.services.automation.refresh_one(space["id"])
    with app.state.db() as db:
        w = db.get(Workspace, space["id"])
        assert w.refresh_status == "session_cookie_missing" and w.refresh_next_at > now() + 21000
        assert db.get(Node, 1).health_status == "unknown"
