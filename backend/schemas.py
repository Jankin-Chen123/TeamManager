from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NodeInput(Input):
    name: str = Field(min_length=1, max_length=120)
    scheme: Literal["http", "https", "socks5"] = "http"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")
    clear_auth: bool = False
    enabled: bool = True
    note: str = Field(default="", max_length=2000)

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        import ipaddress
        import re

        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value):
                raise ValueError("只填写主机名或 IP，不包含协议、端口和路径")
        return value


class WorkspaceInput(Input):
    seat_limit: int = Field(0, ge=0, le=100000)
    auto_refresh: bool = True
    credential_type: Literal["admin_api", "web_session"] = "admin_api"
    web_access_token: SecretStr = Field(default=SecretStr(""), max_length=32768)
    session_json: SecretStr = Field(default=SecretStr(""), max_length=262144)
    session_token: SecretStr = Field(default=SecretStr(""), max_length=32768)
    name: str = Field(min_length=1, max_length=120)
    workspace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    admin_key: SecretStr = SecretStr("")
    node_id: int
    enabled: bool = True
    interval_minutes: int = Field(0, ge=0, le=10080)
    note: str = Field(default="", max_length=2000)


class LoginInput(Input):
    password: SecretStr


class PasswordInput(Input):
    current_password: SecretStr
    new_password: SecretStr = Field(min_length=12, max_length=256)


class InviteInput(Input):
    emails: list[EmailStr] = Field(min_length=1, max_length=200)
    seat_type: Literal["chatgpt", "codex"] | None = None


class ConfirmInput(Input):
    confirm: str = Field(min_length=1, max_length=320)


class SettingsInput(Input):
    request_timeout: float = Field(ge=1, le=120)
    concurrency: int = Field(ge=1, le=16)
    log_retention_days: int = Field(ge=0, le=3650)


class CDKInput(Input):
    workspace_id: int | None = None  # Accepted for older clients; never binds new codes.
    count: int = Field(1, ge=1, le=200)
    label: str = Field("", max_length=120)
    expires_at: float | None = Field(None, gt=0, allow_inf_nan=False)
    bound_email: EmailStr | None = None


class RedeemInput(Input):
    code: SecretStr = Field(min_length=20, max_length=128)
    email: EmailStr


class RedemptionStatusInput(Input):
    receipt: SecretStr = Field(min_length=20, max_length=128)
