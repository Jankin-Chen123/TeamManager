from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TM_", env_file=".env", extra="ignore")
    master_key: SecretStr
    admin_password: SecretStr
    database_url: str = "sqlite:///data/teammanager.db"
    cookie_secure: bool = False
    cookie_path: str = "/"
    request_timeout: float = Field(20, ge=1, le=120)
    concurrency: int = Field(4, ge=1, le=16)
    session_hours: int = Field(12, ge=1, le=168)
    frontend_dir: Path = Path("frontend/dist")
    public_redemption: bool = True
    backup_hours: int = Field(24, ge=0, le=8760)
    backup_keep: int = Field(14, ge=1, le=3650)
    backup_dir: Path = Path("backups")

    def prepare(self):
        if len(self.admin_password.get_secret_value()) < 12:
            raise ValueError("TM_ADMIN_PASSWORD 至少需要 12 个字符")
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
