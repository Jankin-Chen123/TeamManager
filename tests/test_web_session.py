import base64
import json
import time
from types import SimpleNamespace

import pytest

from backend.models import Workspace
from backend.upstream import GatewayResult, UpstreamError
from backend.web_session import WebSessionGateway, import_credentials, normalize_page
from tests.conftest import login, seed
from tests.test_gateway import make_gateway, network as network
from tests.test_management import wait_job


def session_token(account="workspace-a", expiry=None):
    data = {
        "exp": expiry if expiry is not None else time.time() + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


@pytest.mark.parametrize("scheme", ["http", "https", "socks5"])
async def test_real_web_request_uses_proxy_no_env_and_verified_tls(network, monkeypatch, scheme):
    monkeypatch.setattr("backend.web_session.WEB_BASE", f"https://workspace.invalid:{network['target_port']}")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    gateway, node, _ = make_gateway(network, scheme)
    web = WebSessionGateway(gateway.vault, 2, network["ca_file"])
    result = await web.request(
        node, "GET", "/backend-api/accounts/workspace-a/users", token="web-secret", account="workspace-a"
    )
    assert result["data"] == []
    assert network["routes"] == [(scheme, f"workspace.invalid:{network['target_port']}")]
    request = network["requests"][0].lower()
    assert "authorization: bearer web-secret" in request
    assert "chatgpt-account-id: workspace-a" in request
    assert "proxy-authorization" not in request
    node.port = 1
    count = len(network["requests"])
    with pytest.raises(UpstreamError) as exc:
        await web.request(node, "GET", "/backend-api/accounts/workspace-a/users", token="web-secret")
    assert exc.value.code in {"proxy_connection", "timeout"}
    assert len(network["requests"]) == count


async def test_web_bad_tls_and_redirect_fail_closed(network, monkeypatch):
    monkeypatch.setattr("backend.web_session.WEB_BASE", f"https://workspace.invalid:{network['target_port']}")
    gateway, node, _ = make_gateway(network)
    with pytest.raises(UpstreamError) as exc:
        await WebSessionGateway(gateway.vault, 2).request(node, "GET", "/test", token="secret")
    assert exc.value.code == "tls_error"
    assert network["requests"] == []
    network["status"] = 302
    with pytest.raises(UpstreamError) as exc:
        await WebSessionGateway(gateway.vault, 2, network["ca_file"]).request(
            node, "POST", "/test", token="secret"
        )
    assert exc.value.code == "redirect_blocked" and exc.value.uncertain
    assert len(network["requests"]) == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "session_expired"),
        (403, "permission_denied"),
        (404, "unavailable"),
        (429, "rate_limited"),
        (500, "upstream_error"),
    ],
)
async def test_web_error_classification_and_single_write_attempt(network, monkeypatch, status, code):
    monkeypatch.setattr("backend.web_session.WEB_BASE", f"https://workspace.invalid:{network['target_port']}")
    gateway, node, _ = make_gateway(network)
    network["status"] = status
    with pytest.raises(UpstreamError) as exc:
        await WebSessionGateway(gateway.vault, 2, network["ca_file"]).request(
            node, "POST", "/test", token="secret"
        )
    assert exc.value.code == code
    assert exc.value.uncertain == (status >= 500)
    assert "do-not-echo-secrets" not in json.dumps(exc.value.public())
    assert len(network["requests"]) == 1


async def test_web_write_timeout_never_retries(network, monkeypatch):
    monkeypatch.setattr("backend.web_session.WEB_BASE", f"https://workspace.invalid:{network['target_port']}")
    gateway, node, _ = make_gateway(network)
    network["delay"] = 0.25
    with pytest.raises(UpstreamError) as exc:
        await WebSessionGateway(gateway.vault, 0.1, network["ca_file"]).request(
            node, "POST", "/test", token="secret"
        )
    assert exc.value.code == "timeout" and exc.value.uncertain
    assert len(network["requests"]) == 1


def test_import_rejects_pat_expiry_account_mismatch_and_redacts_values():
    token = session_token()
    imported, cookie, expiry = import_credentials(
        "",
        json.dumps({"accessToken": token, "account": {"id": "workspace-a"}, "sessionToken": "cookie-secret"}),
        "",
        "workspace-a",
    )
    assert imported == token and cookie == "cookie-secret" and expiry > time.time()
    for value in ["at-private-secret", "sk-private-secret"]:
        with pytest.raises(ValueError) as exc:
            import_credentials(value, "", "", "workspace-a")
        assert value not in str(exc.value)
    with pytest.raises(ValueError, match="过期"):
        import_credentials(session_token(expiry=1), "", "", "workspace-a")
    with pytest.raises(ValueError, match="不匹配"):
        import_credentials(session_token("other"), "", "", "workspace-a")
    with pytest.raises(ValueError):
        import_credentials("", "{not-json-secret}", "", "workspace-a")


async def test_web_adapter_pagination_detail_invite_resend_and_no_guessed_revoke(monkeypatch):
    web = WebSessionGateway(SimpleNamespace(decrypt=lambda value: value), 2)
    w = SimpleNamespace(key_enc=session_token(), workspace_id="workspace-a")
    calls = []

    async def request(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path.endswith("/users"):
            offset = kwargs["params"]["offset"]
            return GatewayResult(
                {
                    "items": [
                        {
                            "id": "user-" + str(offset),
                            "email": "a@example.com",
                            "role": "standard-user",
                            "secret": "hidden",
                        }
                    ],
                    "total": 2,
                },
                "req_list",
            )
        if method == "GET":
            return GatewayResult({"account_invites": [{"id": "invite-1", "email_address": "a@example.com"}]})
        if method == "POST":
            assert kwargs["body"] == {
                "email_addresses": ["a@example.com"],
                "role": "standard-user",
                "resend_emails": True,
            }
            return GatewayResult(
                {"account_invites": [{"id": "invite-1", "email_address": "a@example.com"}]}, "req_invite"
            )
        return GatewayResult({})

    monkeypatch.setattr(web, "request", request)
    first = await web.workspace(w, None, "GET", "users", params={"limit": 1})
    assert first["has_more"] and first["last_id"] == "1"
    second = await web.workspace(w, None, "GET", "users", params={"limit": 1, "after": "1"})
    assert second["data"][0]["id"] == "user-1" and not second["has_more"]
    detail = await web.workspace(w, None, "GET", "users/user-1")
    assert detail["role"] == "member"
    invited = await web.workspace(w, None, "POST", "invites", body={"email": "a@example.com"})
    assert invited["id"] == "invite-1" and invited.request_id == "req_invite"
    await web.workspace(w, None, "POST", "invites/invite-1/resend")
    await web.workspace(w, None, "DELETE", "users/user-1")
    before = len(calls)
    with pytest.raises(UpstreamError, match="operation_unsupported"):
        await web.workspace(w, None, "DELETE", "invites/invite-1")
    assert len(calls) == before
    with pytest.raises(UpstreamError, match="invalid_cursor"):
        await web.workspace(w, None, "GET", "users", params={"after": "arbitrary"})


async def test_web_configuration_jobs_refresh_and_secret_boundaries(installation, monkeypatch):
    app, client, _ = installation
    await login(client)
    node, space = await seed(client)
    token = session_token()
    body = {
        "name": space["name"],
        "workspace_id": space["workspace_id"],
        "node_id": node["id"],
        "credential_type": "web_session",
        "session_json": json.dumps(
            {"accessToken": token, "account": {"id": space["workspace_id"]}, "sessionToken": "cookie-secret"}
        ),
    }
    result = await client.put(f"/api/workspaces/{space['id']}", json=body)
    assert result.status_code == 200
    assert result.json()["credential_type"] == "web_session" and result.json()["has_session_cookie"]
    assert result.json()["last_status"] == "not_probed"
    changed = body | {"session_json": "", "workspace_id": "different-space"}
    rejected = await client.put(f"/api/workspaces/{space['id']}", json=changed)
    assert rejected.status_code == 422
    with app.state.db() as db:
        w = db.get(Workspace, space["id"])
        assert w.key_enc != token and w.session_token_enc != "cookie-secret"
        assert app.state.vault.decrypt(w.session_token_enc) == "cookie-secret"
    calls = []
    newer = session_token(expiry=time.time() + 7200)

    async def request(self, n, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/auth/session":
            assert kwargs["cookie"] == "cookie-secret" and not kwargs.get("token")
            return GatewayResult({"accessToken": newer, "account": {"id": "workspace-a"}}, "req_refresh")
        if method == "GET":
            return GatewayResult(
                {
                    "items": [
                        {
                            "id": "user-1",
                            "email": "a@example.com",
                            "role": "account-owner",
                            "secret": "hidden",
                        }
                    ],
                    "total": 1,
                },
                "req_users",
            )
        return GatewayResult(
            {"account_invites": [{"id": "invite-1", "email": "new@example.com"}]}, "req_write"
        )

    monkeypatch.setattr(WebSessionGateway, "request", request)
    response = await client.get(f"/api/workspaces/{space['id']}/users")
    assert response.json()["data"][0]["role"] == "owner" and "hidden" not in response.text
    response = await client.post(
        f"/api/workspaces/{space['id']}/remove/user-1", json={"confirm": "a@example.com"}
    )
    assert response.status_code == 409
    response = await client.post(
        f"/api/workspaces/{space['id']}/invite", json={"emails": ["new@example.com"]}
    )
    job = await wait_job(client, response.json()["job_id"])
    assert job["status"] == "success" and job["items"][0]["result"]["request_id"] == "req_write"
    refreshed = await client.post(f"/api/workspaces/{space['id']}/refresh-session")
    assert refreshed.status_code == 200
    with app.state.db() as db:
        assert app.state.vault.decrypt(db.get(Workspace, space["id"]).key_enc) == newer
    for path in ["/api/workspaces", "/api/jobs", "/api/audit"]:
        response = await client.get(path)
        assert (
            token not in response.text and newer not in response.text and "cookie-secret" not in response.text
        )
    invalid = body | {"session_json": "", "web_access_token": "at-private-secret"}
    response = await client.put(f"/api/workspaces/{space['id']}", json=invalid)
    assert response.status_code == 422 and "at-private-secret" not in response.text


def test_unpaginated_invites_and_malformed_page():
    page = normalize_page(
        GatewayResult({"account_invites": [{"email_address": "a@example.com"}]}), "invites", 0, 1
    )
    assert page["has_more"] and page["data"][0]["id"] == "a@example.com"
    with pytest.raises(UpstreamError, match="invalid_response"):
        normalize_page({"items": [None]}, "users", 0, 100)
