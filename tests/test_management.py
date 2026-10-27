import asyncio
import sqlite3
import zipfile

import pytest
from sqlalchemy import select

from backend.app import create_app
from backend.models import Job, JobItem, Node, Workspace
from backend.upstream import UpstreamError
from cryptography.fernet import Fernet
from pydantic import SecretStr
from scripts.backup import backup
from tests.conftest import login, seed


async def wait_job(client, id):
    for _ in range(200):
        jobs = (await client.get("/api/jobs")).json()
        job = next(j for j in jobs if j["id"] == id)
        if job["status"] not in {"running", "queued"}:
            return job
        await asyncio.sleep(0.01)
    pytest.fail("Background job did not finish")


async def test_login_csrf_and_validation_redaction(installation):
    app, client, _ = installation
    assert (await client.get("/api/nodes")).status_code == 401
    assert (await client.post("/api/login", json={"password": "test-password-123456"})).status_code == 403
    await login(client)
    saved = client.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/nodes", json={})).status_code == 403
    client.headers["X-CSRF-Token"] = saved
    result = await client.post(
        "/api/nodes",
        json={"name": "secret", "host": "https://bad/path", "port": 1, "password": "must-not-echo"},
    )
    assert result.status_code == 422
    assert "must-not-echo" not in result.text
    assert "https://bad" not in result.text


async def test_configuration_vault_and_binding_guards(installation):
    app, client, settings = installation
    await login(client)
    node, space = await seed(client)
    response = await client.get("/api/nodes")
    assert "proxy-password-secret" not in response.text
    assert "proxy-user" not in response.text
    assert "admin-key-secret" not in (await client.get("/api/workspaces")).text
    with app.state.db() as db:
        encrypted = db.get(Node, node["id"]).password_enc
        assert encrypted != "proxy-password-secret"
        assert app.state.vault.decrypt(encrypted) == "proxy-password-secret"
        old_key = db.get(Workspace, space["id"]).key_enc
    updated = await client.put(
        f"/api/workspaces/{space['id']}",
        json={
            "name": "改名空间",
            "workspace_id": space["workspace_id"],
            "node_id": node["id"],
            "admin_key": "",
        },
    )
    assert updated.status_code == 200
    with app.state.db() as db:
        assert db.get(Workspace, space["id"]).key_enc == old_key
    assert (await client.delete(f"/api/nodes/{node['id']}")).status_code == 409
    duplicate = await client.post(
        "/api/workspaces",
        json={
            "name": "重复",
            "workspace_id": space["workspace_id"],
            "node_id": node["id"],
            "admin_key": "dont-echo",
        },
    )
    assert duplicate.status_code == 409
    assert "dont-echo" not in duplicate.text
    result = await client.request("DELETE", f"/api/workspaces/{space['id']}", json={"confirm": "wrong"})
    assert result.status_code == 400
    result = await client.request("DELETE", f"/api/workspaces/{space['id']}", json={"confirm": "改名空间"})
    assert result.status_code == 200
    assert (await client.delete(f"/api/nodes/{node['id']}")).status_code == 200
    records = (await client.get("/api/audit")).json()
    assert records[0]["node_name"] == "出口 A"
    assert (await client.get("/api/health")).json()["schema"] == "0006"


async def test_probe_permission_separation_and_pagination(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)

    async def test_node(node):
        return "203.0.113.4"

    calls = []

    async def upstream(workspace, node, method, resource, params=None, body=None):
        calls.append((method, resource, params))
        if resource == "invites":
            raise UpstreamError("permission_denied")
        if params.get("after"):
            return {
                "data": [{"id": "user-b", "email": "b@example.com"}],
                "has_more": False,
                "last_id": "user-b",
            }
        return {
            "data": [{"id": "user-a", "email": "a@example.com", "secret": "never-echo"}],
            "has_more": True,
            "last_id": "user-a",
        }

    monkeypatch.setattr(app.state.services.gateway, "test_node", test_node)
    monkeypatch.setattr(app.state.services.gateway, "workspace", upstream)
    result = (await client.post(f"/api/workspaces/{space['id']}/probe")).json()
    assert result["status"] == "partial"
    assert result["details"]["users"]["code"] == "success"
    assert result["details"]["invites"]["code"] == "permission_denied"
    first = await client.get(f"/api/workspaces/{space['id']}/users")
    assert "never-echo" not in first.text
    assert first.json()["has_more"] is True
    second = await client.get(f"/api/workspaces/{space['id']}/users?after=user-a")
    assert second.json()["data"][0]["id"] == "user-b"
    assert calls[-1][2]["after"] == "user-a"


async def test_invite_jobs_timeout_never_retries_and_restart_recovery(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    calls = []

    async def upstream(workspace, node, method, resource, params=None, body=None):
        calls.append(body["email"])
        assert body["role"] == "member"
        if body["email"].startswith("unknown"):
            raise UpstreamError("timeout", uncertain=True)
        return {"id": "invite-1", "email": body["email"], "status": "pending"}

    monkeypatch.setattr(app.state.services.gateway, "workspace", upstream)
    result = await client.post(
        f"/api/workspaces/{space['id']}/invite",
        json={"emails": ["ok@example.com", "unknown@example.com", "ok@example.com"]},
    )
    job = await wait_job(client, result.json()["job_id"])
    assert job["status"] == "uncertain"
    assert [i["status"] for i in job["items"]] == ["success", "uncertain"]
    assert calls == ["ok@example.com", "unknown@example.com"]
    with app.state.db.begin() as db:
        interrupted = Job(workspace_id=space["id"], kind="invite", status="running")
        db.add(interrupted)
        db.flush()
        id = interrupted.id
        db.add(JobItem(job_id=id, target="inflight@example.com", action="invite", status="running"))
        db.add(JobItem(job_id=id, target="queued@example.com", action="invite", status="queued"))
    app.state.services.recover()
    with app.state.db() as db:
        assert db.get(Job, id).status == "uncertain"
        assert list(db.scalars(select(JobItem.status).where(JobItem.job_id == id))) == [
            "uncertain",
            "cancelled",
        ]
    assert len(calls) == 2


async def test_batch_stops_on_rate_limit_and_member_protections(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    calls = []

    async def upstream(workspace, node, method, resource, params=None, body=None):
        calls.append((method, resource))
        if method == "GET":
            return {"id": "user-1", "email": "owner@example.com", "role": "owner", "is_scim_managed": False}
        raise UpstreamError("rate_limited", retry_after=30)

    monkeypatch.setattr(app.state.services.gateway, "workspace", upstream)
    result = await client.post(
        f"/api/workspaces/{space['id']}/invite", json={"emails": ["one@example.com", "two@example.com"]}
    )
    job = await wait_job(client, result.json()["job_id"])
    assert [i["status"] for i in job["items"]] == ["failed", "cancelled"]
    assert len(calls) == 1
    result = await client.post(
        f"/api/workspaces/{space['id']}/remove/user-1", json={"confirm": "owner@example.com"}
    )
    assert result.status_code == 409
    assert all(method != "DELETE" for method, _ in calls)


async def test_mutation_paths_and_confirmation(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    calls = []

    async def upstream(workspace, node, method, resource, params=None, body=None):
        calls.append((method, resource))
        if method == "GET":
            return {
                "id": "target-1",
                "email": "member@example.com",
                "role": "member",
                "is_scim_managed": False,
            }
        return {"id": "target-1", "deleted": True}

    monkeypatch.setattr(app.state.services.gateway, "workspace", upstream)
    for action, path, method in [
        ("remove", "users/target-1", "DELETE"),
        ("revoke", "invites/target-1", "DELETE"),
        ("resend", "invites/target-1/resend", "POST"),
    ]:
        bad = await client.post(f"/api/workspaces/{space['id']}/{action}/target-1", json={"confirm": "wrong"})
        assert bad.status_code == 400
        result = await client.post(
            f"/api/workspaces/{space['id']}/{action}/target-1", json={"confirm": "member@example.com"}
        )
        job = await wait_job(client, result.json()["job_id"])
        assert job["status"] == "success"
        assert calls[-1] == (method, path)


async def test_restart_and_consistent_backup_restore(installation, tmp_path):
    app, client, settings = installation
    await login(client)
    _, space = await seed(client)
    env = tmp_path / ".env"
    env.write_text(
        f"TM_DATABASE_URL={settings.database_url}\nTM_MASTER_KEY={settings.master_key.get_secret_value()}\n",
        encoding="utf-8",
    )
    archive = backup(env, tmp_path / "backups", include_secrets=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(tmp_path / "restored")
    restored_file = tmp_path / "restored" / "teammanager.db"
    with sqlite3.connect(restored_file) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT name FROM workspaces").fetchone()[0] == "测试空间"
    restored = create_app(settings.model_copy(update={"database_url": f"sqlite:///{restored_file}"}))
    async with restored.router.lifespan_context(restored):
        with restored.state.db() as db:
            w = db.get(Workspace, space["id"])
            assert restored.state.vault.decrypt(w.key_enc) == "admin-key-secret"
    # Migration reruns are idempotent and preserve existing rows.
    second = create_app(settings)
    async with second.router.lifespan_context(second):
        with second.state.db() as db:
            assert db.get(Workspace, space["id"]).name == "测试空间"
    with pytest.raises(RuntimeError, match="TM_MASTER_KEY"):
        create_app(settings.model_copy(update={"master_key": SecretStr(Fernet.generate_key().decode())}))


async def test_password_change_invalidates_all_sessions(installation):
    _, client, _ = installation
    await login(client)
    result = await client.post(
        "/api/password",
        json={"current_password": "test-password-123456", "new_password": "new-password-123456"},
    )
    assert result.status_code == 200
    assert (await client.get("/api/me")).status_code == 401
    result = await client.post(
        "/api/login", json={"password": "new-password-123456"}, headers={"X-TeamManager": "1"}
    )
    assert result.status_code == 200


async def test_scheduler_uses_read_only_probes_and_deduplicates(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    _, space = await seed(client)
    with app.state.db.begin() as db:
        db.get(Workspace, space["id"]).interval_minutes = 1
    calls = []

    async def test_node(node):
        return "203.0.113.1"

    async def upstream(workspace, node, method, resource, params=None, body=None):
        calls.append((method, resource))
        return {"data": [], "has_more": False, "last_id": None}

    monkeypatch.setattr(app.state.services.gateway, "test_node", test_node)
    monkeypatch.setattr(app.state.services.gateway, "workspace", upstream)
    scheduler = asyncio.create_task(app.state.services.scheduler())
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(calls) == 2:
                break
        assert calls == [("GET", "users"), ("GET", "invites")]
        with app.state.db() as db:
            assert db.get(Workspace, space["id"]).last_status == "success"
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
