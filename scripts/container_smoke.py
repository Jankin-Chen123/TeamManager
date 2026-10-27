"""CI-only check: configuration and encrypted credentials survive container replacement."""

import sys
import httpx
from dotenv import dotenv_values

with httpx.Client(
    base_url=sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765", trust_env=False
) as client:
    login = client.post(
        "/api/login",
        json={"password": dotenv_values(".env")["TM_ADMIN_PASSWORD"]},
        headers={"X-TeamManager": "1"},
    )
    login.raise_for_status()
    client.headers["X-CSRF-Token"] = login.json()["csrf"]
    if sys.argv[1] == "seed":
        node = client.post(
            "/api/nodes",
            json={
                "name": "CI node",
                "scheme": "socks5",
                "host": "127.0.0.1",
                "port": 9,
                "username": "ci-user",
                "password": "ci-password",
            },
        )
        node.raise_for_status()
        space = client.post(
            "/api/workspaces",
            json={
                "name": "CI space",
                "workspace_id": "ci-workspace",
                "admin_key": "ci-only-key",
                "node_id": node.json()["id"],
            },
        )
        space.raise_for_status()
        web = client.post(
            "/api/workspaces",
            json={
                "name": "CI web space",
                "workspace_id": "ci-web-workspace",
                "credential_type": "web_session",
                "auto_refresh": False,
                "web_access_token": "ci-web-access-token",
                "session_token": "ci-web-cookie",
                "node_id": node.json()["id"],
            },
        )
        web.raise_for_status()
        generated = client.post(
            "/api/cdks", json={"workspace_id": space.json()["id"], "label": "CI persistence"}
        )
        generated.raise_for_status()
        configured = client.put(
            "/api/settings",
            json={
                "request_timeout": 2,
                "concurrency": 2,
                "log_retention_days": 30,
            },
        )
        configured.raise_for_status()
    else:
        nodes, spaces = client.get("/api/nodes"), client.get("/api/workspaces")
        nodes.raise_for_status()
        spaces.raise_for_status()
        assert nodes.json()[0]["name"] == "CI node"
        assert nodes.json()[0]["has_auth"] is True
        assert spaces.json()[0]["has_key"] is True
        web = next(w for w in spaces.json() if w["name"] == "CI web space")
        assert web["credential_type"] == "web_session" and web["has_session_cookie"]
        assert web["capabilities"]["revoke"] is False and web["auto_refresh"] is False
        codes = client.get("/api/cdks")
        codes.raise_for_status()
        assert codes.json()[0]["label"] == "CI persistence"
        assert codes.json()[0]["status"] == "available"
        response = client.post(f"/api/workspaces/{web['id']}/refresh-session")
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "proxy_connection"
        assert "ci-web-cookie" not in response.text + spaces.text
        assert "ci-web-access-token" not in response.text + spaces.text
        configured = client.get("/api/settings")
        configured.raise_for_status()
        assert configured.json()["request_timeout"] == 2
        assert configured.json()["concurrency"] == 2
        assert configured.json()["log_retention_days"] == 30
        response = client.post(f"/api/nodes/{nodes.json()[0]['id']}/test")
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "proxy_connection"
        assert "ci-password" not in nodes.text + spaces.text + response.text
        assert "ci-only-key" not in spaces.text
    print("Container configuration check passed:", sys.argv[1])
