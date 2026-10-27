import pytest_asyncio
import httpx
from cryptography.fernet import Fernet

from backend.app import create_app
from backend.config import Settings


@pytest_asyncio.fixture
async def installation(tmp_path):
    settings = Settings(
        _env_file=None,
        backup_hours=0,
        master_key=Fernet.generate_key().decode(),
        admin_password="test-password-123456",
        database_url=f"sqlite:///{tmp_path}/test.db",
        frontend_dir=tmp_path / "missing",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield app, client, settings


async def login(client):
    response = await client.post(
        "/api/login", json={"password": "test-password-123456"}, headers={"X-TeamManager": "1"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf"]


async def seed(client, *, scheme="http", port=8888, name="出口 A", workspace_id="workspace-a"):
    node = await client.post(
        "/api/nodes",
        json={
            "name": name,
            "scheme": scheme,
            "host": "127.0.0.1",
            "port": port,
            "username": "proxy-user",
            "password": "proxy-password-secret",
        },
    )
    assert node.status_code == 200, node.text
    space = await client.post(
        "/api/workspaces",
        json={
            "name": "测试空间",
            "workspace_id": workspace_id,
            "admin_key": "admin-key-secret",
            "node_id": node.json()["id"],
        },
    )
    assert space.status_code == 200, space.text
    return node.json(), space.json()
