"""ChatGPT website protocol adapter; credentials never leave the fixed ChatGPT origin.

Route reference: Redmig110/Team-Workflow, a43c5c3, team_protocol/chatgpt.py (MIT).
"""

import asyncio
import base64
import json
import time
import math
from urllib.parse import quote

from curl_cffi.requests import AsyncSession, RequestsError
from curl_cffi import CurlOpt
import certifi

from backend.upstream import GatewayResult, UpstreamError, proxy_url, safe_request_id

WEB_BASE = "https://chatgpt.com"


def token_metadata(token):
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if not isinstance(data, dict):
            return None, ""
        expiry = data.get("exp")
        auth = data.get("https://api.openai.com/auth", {})
        return (
            expiry
            if isinstance(expiry, (int, float))
            and not isinstance(expiry, bool)
            and 0 < expiry < 2**53
            and math.isfinite(expiry)
            else None,
            auth.get("chatgpt_account_id", "") if isinstance(auth, dict) else "",
        )
    except (ValueError, IndexError, TypeError):
        return None, ""


def import_credentials(token, session_json, cookie, workspace_id):
    account = ""
    if session_json:
        try:
            data = json.loads(session_json)
            if not isinstance(data, dict):
                raise ValueError()
            token = data.get("accessToken") or data.get("access_token") or ""
            cookie = data.get("sessionToken") or data.get("session_token") or cookie
            account = (data.get("account") or {}).get("id", "")
        except (ValueError, TypeError, AttributeError):
            raise ValueError("会话 JSON 格式不正确，请导入 /api/auth/session 返回的完整对象") from None
    if not isinstance(token, str) or not isinstance(cookie, str):
        raise ValueError("会话凭据必须是文本")
    if session_json and not token:
        raise ValueError("会话 JSON 中没有 accessToken，请先登录并切换到目标空间")
    token, cookie = token.strip(), cookie.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if any(c.isspace() for c in token + cookie):
        raise ValueError("会话凭据不能包含空白字符")
    if token.startswith(("at-", "sk-")):
        raise ValueError(
            "这里需要网页登录会话的 accessToken，不能使用 Credentials 创建的 PAT 或 Platform API key"
        )
    expiry, claim_account = token_metadata(token)
    if account and account != workspace_id:
        raise ValueError("导入会话所属空间与 Workspace ID 不一致，请在 ChatGPT 中切换到目标空间后重新获取")
    if claim_account and claim_account != workspace_id:
        raise ValueError("网页登录 token 所属空间不匹配，请切换到目标空间后重新获取")
    if expiry is not None and expiry <= time.time():
        raise ValueError("网页登录 token 已过期，请重新获取会话")
    return token, cookie, expiry


def normalize_record(row, resource):
    if not isinstance(row, dict):
        raise UpstreamError("invalid_response")
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    result = dict(user) | row
    result["id"] = row.get("id") or row.get("user_id") or user.get("id")
    result["email"] = row.get("email") or row.get("email_address") or user.get("email")
    role = result.get("role")
    result["role"] = {"account-owner": "owner", "account-admin": "admin", "standard-user": "member"}.get(
        role, role
    )
    if resource == "invites":
        result["status"] = row.get("status", "pending")
        # Some website invitation responses use email as their only stable identifier.
        result["id"] = result.get("id") or result.get("email")
    return result


def normalize_page(raw, resource, offset, limit):
    rows = next(
        (
            raw[k]
            for k in ("items", "users", "account_invites", "invites", "data")
            if isinstance(raw.get(k), list)
        ),
        None,
    )
    if rows is None or any(not isinstance(row, dict) for row in rows):
        raise UpstreamError("invalid_response", request_id=getattr(raw, "request_id", ""))
    total = raw.get("total", raw.get("total_count"))
    more = offset + len(rows) < total if isinstance(total, int) else (len(rows) >= limit)
    if isinstance(raw.get("has_more"), bool):
        more = raw["has_more"]
    if more and not rows:
        raise UpstreamError("invalid_response")
    return GatewayResult(
        {
            "data": [normalize_record(row, resource) for row in rows],
            "has_more": more,
            "last_id": str(offset + len(rows)) if more else None,
        },
        getattr(raw, "request_id", ""),
    )


class WebSessionGateway:
    def __init__(self, vault, timeout, verify=True):
        self.vault, self.timeout, self.verify = vault, timeout, verify

    async def request(self, node, method, path, *, token="", account="", cookie="", params=None, body=None):
        proxy = proxy_url(node, self.vault)
        if node.scheme == "socks5":
            proxy = "socks5h://" + proxy.removeprefix("socks5://")
        headers = {"Accept": "application/json", "Origin": WEB_BASE, "Referer": WEB_BASE + "/"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if account:
            headers["chatgpt-account-id"] = account
        if cookie:
            headers["Cookie"] = "__Secure-next-auth.session-token=" + cookie
        write = method != "GET"
        for attempt in range(1 if write else 2):
            try:
                # libcurl still reads NO_PROXY unless explicitly overridden, even with trust_env=False.
                options = {
                    CurlOpt.NOPROXY: "",
                    CurlOpt.PROXY_SSL_VERIFYPEER: 1,
                    CurlOpt.PROXY_SSL_VERIFYHOST: 2,
                    CurlOpt.PROXY_CAINFO: self.verify if isinstance(self.verify, str) else certifi.where(),
                }
                async with AsyncSession(
                    trust_env=False, impersonate="chrome", verify=self.verify, curl_options=options
                ) as session:
                    response = await session.request(
                        method,
                        WEB_BASE + path,
                        headers=headers,
                        proxy=proxy,
                        params=params,
                        json=body,
                        timeout=self.timeout,
                        allow_redirects=False,
                    )
            except RequestsError as exc:
                code = int(exc.code)
                category = {
                    5: "proxy_connection",
                    6: "proxy_connection",
                    7: "proxy_connection",
                    28: "timeout",
                    35: "tls_error",
                    60: "tls_error",
                    67: "proxy_auth",
                    97: "proxy_auth",
                }.get(code, "network_error")
                if not write and attempt == 0 and category in {"proxy_connection", "timeout"}:
                    await asyncio.sleep(0.3)
                    continue
                raise UpstreamError(category, uncertain=write and code not in {5, 6, 7, 60, 67, 97}) from None
            status = response.status_code
            rid = safe_request_id(response.headers.get("x-request-id", ""))
            if 300 <= status < 400:
                raise UpstreamError("redirect_blocked", uncertain=write, request_id=rid)
            if status >= 400:
                category = {
                    401: "session_expired",
                    403: "permission_denied",
                    404: "unavailable",
                    407: "proxy_auth",
                    429: "rate_limited",
                }.get(status, "upstream_error" if status >= 500 else "request_rejected")
                if status == 403 and "text/html" in response.headers.get("content-type", ""):
                    category = "web_verification"
                retry = response.headers.get("Retry-After", "")
                retry = int(retry) if retry.isdigit() else None
                if (
                    not write
                    and attempt == 0
                    and status in {429, 502, 503, 504}
                    and (retry is None or retry <= 2)
                ):
                    await asyncio.sleep(retry if retry is not None else 0.5)
                    continue
                raise UpstreamError(
                    category, uncertain=write and status >= 500, retry_after=retry, request_id=rid
                )
            if status == 204:
                return GatewayResult({}, rid)
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return GatewayResult(data, rid)
            except ValueError:
                raise UpstreamError("invalid_response", uncertain=write, request_id=rid) from None

    async def workspace(self, workspace, node, method, resource, params=None, body=None):
        token = self.vault.decrypt(workspace.key_enc)
        if token.startswith(("at-", "sk-")):
            raise UpstreamError("wrong_credential")
        expiry, _ = token_metadata(token)
        if not token or (expiry and expiry <= time.time()):
            raise UpstreamError("session_expired")
        account = workspace.workspace_id
        base = "/backend-api/accounts/" + quote(account, safe="")
        parts = resource.split("/")
        kind = parts[0]
        if kind not in {"users", "invites"}:
            raise UpstreamError("operation_unsupported")
        if method == "GET":
            params = params or {}
            after = params.get("after", "0")
            if not str(after).isdigit() or int(after) > 1000000:
                raise UpstreamError("invalid_cursor")
            offset, limit = int(after), params.get("limit", 100)
            seen = set()
            for _ in range(100 if len(parts) > 1 else 1):
                raw = await self.request(
                    node,
                    "GET",
                    base + "/" + kind,
                    token=token,
                    account=account,
                    params={"limit": limit, "offset": offset},
                )
                page = normalize_page(raw, kind, offset, limit)
                if len(parts) == 1:
                    return page
                from urllib.parse import unquote

                target = unquote(parts[1])
                for row in page["data"]:
                    if str(row.get("id")) == target:
                        return GatewayResult(row, raw.request_id)
                signature = tuple(str(row.get("id")) for row in page["data"])
                if not page["has_more"]:
                    raise UpstreamError("unavailable", request_id=raw.request_id)
                if signature in seen:
                    raise UpstreamError("invalid_response")
                seen.add(signature)
                offset = int(page["last_id"])
            raise UpstreamError("lookup_limit")
        if method == "POST" and kind == "invites" and len(parts) == 1:
            if body.get("seat_type"):
                raise UpstreamError("operation_unsupported")
            raw = await self.request(
                node,
                "POST",
                base + "/invites",
                token=token,
                account=account,
                body={"email_addresses": [body["email"]], "role": "standard-user", "resend_emails": True},
            )
            rows = next(
                (raw[k] for k in ("account_invites", "invites", "items") if isinstance(raw.get(k), list)), []
            )
            if rows:
                return GatewayResult(normalize_record(rows[0], "invites"), raw.request_id)
            if raw.get("errored_emails") or raw.get("success") is False:
                raise UpstreamError("request_rejected", request_id=raw.request_id)
            if raw.get("success") is True:
                return GatewayResult({"email": body["email"]}, raw.request_id)
            raise UpstreamError("invalid_response", uncertain=True, request_id=raw.request_id)
        if method == "POST" and kind == "invites" and len(parts) == 3 and parts[2] == "resend":
            record = await self.workspace(workspace, node, "GET", "/".join(parts[:2]))
            if not record.get("email"):
                raise UpstreamError("invalid_response")
            return await self.workspace(workspace, node, "POST", "invites", body={"email": record["email"]})
        if method == "DELETE" and kind == "users" and len(parts) == 2:
            return await self.request(
                node, "DELETE", base + "/users/" + parts[1], token=token, account=account
            )
        # Never invent an unverified destructive route. Enable after observing the website contract.
        raise UpstreamError("operation_unsupported")
