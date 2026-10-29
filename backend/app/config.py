"""应用配置。

所有环境相关参数集中在此处，通过 backend/.env 注入。
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """AI 物管助手运行配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AI 物管助手"
    app_env: str = "development"
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    max_query_days: int = Field(default=366, ge=1, le=3660)
    preview_rows: int = Field(default=20, ge=0, le=100)

    # 数据源适配器开关：rpa / api。
    data_source: str = "rpa"
    rpa_download_dir: Path = Path(".runtime/rpa_downloads")
    rpa_simulate_delay_seconds: float = Field(default=0.0, ge=0.0, le=10.0)

    property_api_base_url: str = ""
    property_api_key: SecretStr = SecretStr("")

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    llm_fallback_enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """启动时尽早暴露非法时区，避免运行期日期计算漂移。"""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"非法时区: {value}") from exc
        return value

    @field_validator("data_source")
    @classmethod
    def normalize_data_source(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def deepseek_key(self) -> str:
        """返回明文 Key，仅在构造 HTTP 请求头时使用。"""
        return self.deepseek_api_key.get_secret_value().strip()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
