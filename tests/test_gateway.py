import asyncio
import base64
import json
import ssl
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest
import pytest_asyncio
import trustme

from backend.security import Vault
from backend.upstream import Gateway, UpstreamError


@pytest_asyncio.fixture
async def network(tmp_path):
    """A real TLS upstream and HTTP CONNECT / SOCKS5 proxies on loopback sockets."""
    ca = trustme.CA()
    cert = ca.issue_cert("workspace.invalid", "127.0.0.1")
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(server_tls)
    client_tls = ssl.create_default_context()
    ca.configure_trust(client_tls)
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_file)
    state = {"requests": [], "routes": [], "status": 200, "delay": 0, "auth_ok": True}
    workers = set()

    async def upstream(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            state["requests"].append(headers)
            if state["delay"]:
                await asyncio.sleep(state["delay"])
            status = state["status"]
            body = json.dumps(
                {"data": [], "has_more": False, "last_id": None, "error": "do-not-echo-secrets"}
            ).encode()
            extra = f"Retry-After: {state['retry_after']}\r\n" if state.get("retry_after") else ""
            writer.write(
                (
                    f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n"
                    "Content-Type: application/json\r\nLocation: https://evil.invalid/\r\n"
                    + extra
                    + "Connection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=server_tls)
    target_port = server.sockets[0].getsockname()[1]

    async def relay(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def proxy(reader, writer, scheme):
        try:
            if scheme in {"http", "https"}:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode()
                auth = base64.b64encode(b"user:p@ss:word").decode()
                if (
                    not state["auth_ok"]
                    or f"Proxy-Authorization: Basic {auth}".lower() not in headers.lower()
                ):
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
                assert headers.startswith("CONNECT ")
                state["routes"].append((scheme, headers.split()[1]))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                version, count = await reader.readexactly(2)
                await reader.readexactly(count)
                assert version == 5
                writer.write(b"\x05\x02")
                await writer.drain()
                _, length = await reader.readexactly(2)
                user = await reader.readexactly(length)
                length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(length)
                if not state["auth_ok"] or user != b"user" or password != b"p@ss:word":
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    return
                writer.write(b"\x01\x00")
                await writer.drain()
                _, command, _, address_type = await reader.readexactly(4)
                assert command == 1
                if address_type == 3:
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode()
                else:
                    host = str(await reader.readexactly(4 if address_type == 1 else 16))
                port = int.from_bytes(await reader.readexactly(2), "big")
                state["routes"].append((scheme, f"{host}:{port}"))
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                await writer.drain()
            remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", target_port)
            await asyncio.gather(relay(reader, remote_writer), relay(remote_reader, writer))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def handler(scheme):
        def spawn(reader, writer):
            task = asyncio.create_task(proxy(reader, writer, scheme))
            workers.add(task)
            task.add_done_callback(workers.discard)

        return spawn

    proxies = {}
    for scheme in ("http", "https", "socks5"):
        proxies[scheme] = await asyncio.start_server(
            handler(scheme), "127.0.0.1", 0, ssl=server_tls if scheme == "https" else None
        )
    state.update(
        ports={s: p.sockets[0].getsockname()[1] for s, p in proxies.items()},
        target_port=target_port,
        tls=client_tls,
        ca_file=str(ca_file),
    )
    yield state
    for listener in [server, *proxies.values()]:
        listener.close()
        await listener.wait_closed()
    for task in list(workers):
        task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


def make_gateway(network, scheme="http", timeout=1):
    vault = Vault(Fernet.generate_key().decode())
    node = SimpleNamespace(
        scheme=scheme,
        host="127.0.0.1",
        port=network["ports"][scheme],
        enabled=True,
        username_enc=vault.encrypt("user"),
        password_enc=vault.encrypt("p@ss:word"),
    )
    workspace = SimpleNamespace(enabled=True, workspace_id="workspace-a", key_enc=vault.encrypt("key-secret"))
    return Gateway(vault, timeout, network["tls"]), node, workspace


@pytest.mark.parametrize("scheme", ["http", "https", "socks5"])
async def test_real_proxy_tunnel_tls_and_remote_dns(network, monkeypatch, scheme):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network, scheme)
    response = await gateway.workspace(workspace, node, "GET", "users")
    assert response["has_more"] is False
    assert network["routes"] == [(scheme, f"workspace.invalid:{network['target_port']}")]
    assert "Authorization: Bearer key-secret" in network["requests"][0]
    assert "Proxy-Authorization" not in network["requests"][0]


async def test_distinct_workspace_routes_and_rebinding(network, monkeypatch):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node_a, workspace = make_gateway(network)
    node_b = SimpleNamespace(**vars(node_a))
    node_b.scheme, node_b.port = "socks5", network["ports"]["socks5"]
    workspace_b = SimpleNamespace(**vars(workspace))
    workspace_b.workspace_id = "workspace-b"
    workspace_b.key_enc = gateway.vault.encrypt("key-b-secret")
    await gateway.workspace(workspace, node_a, "GET", "users")
    await gateway.workspace(workspace_b, node_b, "GET", "invites")
    await gateway.workspace(workspace, node_b, "GET", "users")
    assert [s for s, _ in network["routes"]] == ["http", "socks5", "socks5"]
    assert "/workspace-a/users" in network["requests"][0]
    assert "/workspace-b/invites" in network["requests"][1]
    assert "Bearer key-b-secret" in network["requests"][1]
    assert "/workspace-a/users" in network["requests"][2]


async def test_proxy_dead_or_disabled_never_falls_back_to_direct(network, monkeypatch):
    # The target really is reachable directly. It must receive zero requests when proxy fails.
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://127.0.0.1:{network['target_port']}/v1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    gateway, node, workspace = make_gateway(network)
    node.enabled = False
    with pytest.raises(UpstreamError, match="proxy_disabled"):
        await gateway.workspace(workspace, node, "GET", "users")
    node.enabled, node.port = True, 1
    with pytest.raises(UpstreamError) as exc:
        await gateway.workspace(workspace, node, "GET", "users")
    assert exc.value.code == "proxy_connection"
    assert network["requests"] == []


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_proxy_authentication_failure(network, monkeypatch, scheme):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network, scheme)
    network["auth_ok"] = False
    with pytest.raises(UpstreamError) as exc:
        await gateway.workspace(workspace, node, "GET", "users")
    assert exc.value.code == "proxy_auth"
    assert network["requests"] == []


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "invalid_key"),
        (403, "permission_denied"),
        (404, "unavailable"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (302, "redirect_blocked"),
    ],
)
async def test_error_classification_and_secret_redaction(network, monkeypatch, status, code):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network)
    network["status"] = status
    with pytest.raises(UpstreamError) as exc:
        await gateway.workspace(workspace, node, "POST", "invites", body={"email": "test@example.com"})
    assert exc.value.code == code
    assert "do-not-echo-secrets" not in json.dumps(exc.value.public())
    assert len(network["requests"]) == 1


async def test_tls_verification_is_required(network, monkeypatch):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network)
    gateway.verify = True  # The private test CA must not be trusted by production defaults.
    with pytest.raises(UpstreamError) as exc:
        await gateway.workspace(workspace, node, "GET", "users")
    assert exc.value.code == "tls_error"
    assert network["requests"] == []


async def test_write_timeout_is_uncertain_and_never_retried(network, monkeypatch):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network, timeout=0.1)
    network["delay"] = 0.3
    with pytest.raises(UpstreamError) as exc:
        await gateway.workspace(workspace, node, "POST", "invites", body={"email": "test@example.com"})
    assert exc.value.code == "timeout"
    assert exc.value.uncertain is True
    assert len(network["requests"]) == 1


async def test_rate_limit_cooldown_blocks_followup_requests(network, monkeypatch):
    monkeypatch.setattr("backend.upstream.API_BASE", f"https://workspace.invalid:{network['target_port']}/v1")
    gateway, node, workspace = make_gateway(network)
    network["status"], network["retry_after"] = 429, 30
    for method in ["GET", "POST"]:
        with pytest.raises(UpstreamError) as exc:
            await gateway.workspace(workspace, node, method, "invites")
        assert exc.value.code == "rate_limited"
        assert 29 <= exc.value.retry_after <= 30
    assert len(network["requests"]) == 1
