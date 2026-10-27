"""The only outbound gateway. No direct connection and no credential-bearing redirects."""

import asyncio
import ssl
import math
import time
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

API_BASE = "https://api.chatgpt.com/v1"
IP_URL = "https://api.ipify.org?format=json"

MESSAGES = {
    "seats_full": "发送前核验席位不足，未发送邀请",
    "invite_exists": "发送前已发现该邮箱的成员或邀请，未重复发送",
    "session_expired": "网页登录会话已过期或不被接受，请更新会话 accessToken，或使用已保存的会话 Cookie 刷新",
    "wrong_credential": "网页模式需要登录会话 accessToken，不能使用 Credentials 创建的 PAT 或 Platform API key",
    "web_verification": "网页要求登录或验证，请在浏览器中完成验证后重新导入会话",
    "operation_unsupported": "当前接入方式尚未核实此操作的网页接口，未发送写请求",
    "invalid_cursor": "分页位置无效，请刷新列表",
    "lookup_limit": "记录查找超过分页上限，请缩小范围后重试",
    "session_cookie_missing": "未保存会话 Cookie，请重新导入最新的会话 accessToken",
    "operation_busy": "该空间已有探测任务，请稍后再试",
    "proxy_disabled": "出口节点已停用，请求未发送",
    "workspace_disabled": "工作空间已停用，请求未发送",
    "proxy_auth": "代理认证失败",
    "proxy_connection": "无法连接代理或建立代理隧道",
    "timeout": "连接或上游响应超时",
    "tls_error": "TLS 证书校验失败",
    "invalid_key": "管理员密钥缺失、失效或不被接受",
    "permission_denied": "权限不足或空间不允许此操作",
    "unavailable": "资源不存在或功能未开放，不能据此判断空间失效",
    "rate_limited": "请求受到限流，请稍后再试",
    "upstream_error": "上游服务异常",
    "request_rejected": "上游拒绝请求，请核对参数、成员或邀请状态",
    "redirect_blocked": "已阻止上游重定向",
    "invalid_response": "上游返回格式不符合接口约定",
    "network_error": "网络传输失败",
    "node_cooling": "绑定节点异常，正在冷却等待恢复检查；未发送空间请求",
}


@dataclass
class UpstreamError(Exception):
    code: str
    uncertain: bool = False
    retry_after: int | None = None
    request_id: str = ""

    def public(self):
        return {
            "code": self.code,
            "message": MESSAGES.get(self.code, "请求失败"),
            "uncertain": self.uncertain,
            "retry_after": self.retry_after,
            "request_id": self.request_id,
        }


class GatewayResult(dict):
    def __init__(self, data, request_id=""):
        super().__init__(data)
        self.request_id = request_id


def safe_request_id(value):
    if re.fullmatch(
        r"(?:req_[A-Za-z0-9_-]{1,120}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})", value or ""
    ):
        return value
    return ""


def proxy_url(node, vault):
    if not node.enabled:
        raise UpstreamError("proxy_disabled")
    host = f"[{node.host}]" if ":" in node.host else node.host
    username, password = vault.decrypt(node.username_enc), vault.decrypt(node.password_enc)
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username or password else ""
    return f"{node.scheme}://{auth}{host}:{node.port}"


def transport_error(exc):
    if any(isinstance(arg, ssl.SSLCertVerificationError) for arg in getattr(exc, "args", [])):
        return "tls_error"
    # Use the exception solely to classify it; never expose its string or request URL.
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return "tls_error"
    if isinstance(exc, httpx.ConnectTimeout):
        return "proxy_connection"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ProxyError):
        return (
            "proxy_auth"
            if any(s in str(exc).lower() for s in ("407", "authentication", "invalid username/password"))
            else "proxy_connection"
        )
    if isinstance(exc, httpx.ConnectError):
        return "proxy_connection"
    return "network_error"


class Gateway:
    def __init__(self, vault, timeout=20, verify=True):
        self.vault, self.timeout, self.verify = vault, timeout, verify
        self.cooldowns = {}
        self.prepare = None
        self.observe = None
        self.node_locks = {}

    async def request(self, node, method, url, key=None, params=None, body=None):
        proxy = proxy_url(node, self.vault)
        write = method != "GET"
        # No automatic retries for mutations. HTTPX defaults also disable transport retries.
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                configured_proxy = httpx.Proxy(
                    proxy,
                    ssl_context=(
                        self.verify
                        if node.scheme == "https" and isinstance(self.verify, ssl.SSLContext)
                        else None
                    ),
                )
                async with httpx.AsyncClient(
                    proxy=configured_proxy,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.timeout,
                    verify=self.verify,
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=body,
                        headers={"Authorization": f"Bearer {key}"} if key else {},
                    )
            except httpx.HTTPError as exc:
                code = transport_error(exc)
                if not write and attempt + 1 < attempts and code in {"timeout", "proxy_connection"}:
                    await asyncio.sleep(0.3)
                    continue
                before_send = isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.PoolTimeout)
                )
                raise UpstreamError(code, uncertain=write and not before_send) from None
            status = response.status_code
            request_id = safe_request_id(response.headers.get("x-request-id", ""))
            if 300 <= status < 400:
                raise UpstreamError("redirect_blocked", uncertain=write, request_id=request_id)
            if status >= 400:
                code = {
                    401: "invalid_key",
                    403: "permission_denied",
                    404: "unavailable",
                    407: "proxy_auth",
                    429: "rate_limited",
                }.get(status, "upstream_error" if status >= 500 else "request_rejected")
                retry = response.headers.get("Retry-After", "")
                retry = int(retry) if retry.isdigit() else None
                # Respect long Retry-After by returning it rather than holding a worker indefinitely.
                if (
                    not write
                    and attempt + 1 < attempts
                    and status in {429, 502, 503, 504}
                    and (retry is None or retry <= 2)
                ):
                    await asyncio.sleep(retry if retry is not None else 0.5)
                    continue
                raise UpstreamError(
                    code, uncertain=write and status >= 500, retry_after=retry, request_id=request_id
                )
            if status == 204:
                return GatewayResult({}, request_id)
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return GatewayResult(data, request_id)
            except ValueError:
                raise UpstreamError("invalid_response", uncertain=write, request_id=request_id) from None

    async def workspace(self, workspace, node, method, resource, params=None, body=None):
        if self.prepare:
            async with self.node_locks.setdefault(node.id, asyncio.Lock()):
                await self.prepare(workspace, node)
                try:
                    result = await self._workspace(workspace, node, method, resource, params, body)
                except UpstreamError as exc:
                    self.observe(workspace, node, exc.code)
                    raise
                self.observe(workspace, node, "success")
                return result
        return await self._workspace(workspace, node, method, resource, params, body)

    async def _workspace(self, workspace, node, method, resource, params=None, body=None):
        if not workspace.enabled:
            raise UpstreamError("workspace_disabled")
        remaining = self.cooldowns.get(workspace.workspace_id, 0) - time.monotonic()
        if remaining > 0:
            raise UpstreamError("rate_limited", retry_after=math.ceil(remaining))
        key = self.vault.decrypt(workspace.key_enc)
        path = f"/manage/workspaces/{quote(workspace.workspace_id, safe='')}/{resource}"
        try:
            if getattr(workspace, "credential_type", "admin_api") == "web_session":
                from backend.web_session import WebSessionGateway

                return await WebSessionGateway(self.vault, self.timeout).workspace(
                    workspace, node, method, resource, params, body
                )
            return await self.request(node, method, API_BASE + path, key, params, body)
        except UpstreamError as exc:
            if exc.code == "rate_limited":
                self.cooldowns[workspace.workspace_id] = time.monotonic() + (exc.retry_after or 60)
            raise

    async def test_node(self, node):
        data = await self.request(node, "GET", IP_URL)
        import ipaddress

        try:
            return str(ipaddress.ip_address(data.get("ip", "")))
        except ValueError:
            raise UpstreamError("invalid_response") from None


def clean_record(data, resource):
    """Whitelist official response fields; never reflect arbitrary upstream errors or secrets."""
    keys = {"id", "email", "name", "role", "seat_type", "is_scim_managed", "deleted"}
    if resource == "invites":
        keys |= {"status", "created_at", "last_sent_at"}
    return {
        key: value
        for key, value in data.items()
        if key in keys and isinstance(value, (str, int, bool, type(None)))
    }


def clean_page(data, resource):
    if not isinstance(data.get("data"), list) or not isinstance(data.get("has_more"), bool):
        raise UpstreamError("invalid_response")
    records = [clean_record(item, resource) for item in data["data"] if isinstance(item, dict)]
    cursor = data.get("last_id")
    if data["has_more"] and (not records or not isinstance(cursor, str) or not cursor):
        raise UpstreamError("invalid_response")
    return {"data": records, "has_more": data["has_more"], "last_id": cursor}
