"""应用配置。

所有环境相关参数集中在此处，通过 backend/.env 注入。
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if getattr(sys, "frozen", False):
    # PyInstaller 打包后 __file__ 指向解包临时目录（onefile 会被清理），
    # 因此以 exe 所在目录作为应用根目录，保证 .env 与 .runtime 可写且稳定。
    APP_HOME = Path(sys.executable).resolve().parent
else:
    # 源码运行：应用根目录固定为 backend/，不依赖进程工作目录。
    # Windows 绿色版双击启动时 CWD 可能是启动器目录甚至 System32。
    APP_HOME = Path(__file__).resolve().parents[1]


def _anchor_to_app_home(value: Path) -> Path:
    """把相对路径解析到 backend/ 下，绝对路径保持原样。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (APP_HOME / path)


class Settings(BaseSettings):
    """AI 物管助手运行配置。"""

    model_config = SettingsConfigDict(
        # 显式指定绝对路径：无论从哪个目录启动都能读到同一份 .env。
        env_file=str(APP_HOME / ".env"),
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

    # 多表目录：存在 Excel 时，代码查询优先使用目录中的多表数据。
    table_directory_enabled: bool = True
    data_table_dir: Path = Path(".runtime/rpa_downloads/tables")
    table_max_files: int = Field(default=50, ge=1, le=200)
    table_max_rows: int = Field(default=50_000, ge=1, le=500_000)
    table_max_columns: int = Field(default=100, ge=1, le=500)
    table_upload_max_bytes: int = Field(
        default=20_000_000,
        ge=100_000,
        le=100_000_000,
    )
    table_upload_max_files: int = Field(default=10, ge=1, le=50)
    table_upload_max_uncompressed_bytes: int = Field(
        default=200_000_000,
        ge=1_000_000,
        le=1_000_000_000,
    )
    # 删除全部表必须经过服务端一次性令牌确认；TTL 不宜过长。
    delete_confirmation_ttl_seconds: int = Field(default=120, ge=30, le=600)
    # 前端 8081 直传 FastAPI 时需要的跨域白名单，逗号分隔。
    cors_origins: str = "http://127.0.0.1:8081,http://localhost:8081"

    property_api_base_url: str = ""
    property_api_key: SecretStr = SecretStr("")

    # 大模型 Provider：用户可通过请求体传 provider_type/user_api_key/model_name
    # 覆盖以下默认值（BYOK）；未传时使用这里的系统配置。
    default_llm_provider: str = "deepseek"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    doubao_api_key: SecretStr = SecretStr("")
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_model: str = "doubao-seed-1-6-250615"
    qwen_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    deepseek_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    llm_fallback_enabled: bool = True

    # 生成式 Pandas 查询：模型只负责生成代码，真正执行必须经过沙箱。
    code_query_enabled: bool = True
    code_sandbox_mode: str = "bwrap"
    code_max_retries: int = Field(default=2, ge=0, le=5)
    code_llm_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    code_exec_timeout_seconds: float = Field(default=8.0, gt=0.0, le=60.0)
    code_total_timeout_seconds: float = Field(default=50.0, gt=0.0, le=180.0)
    code_max_code_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    code_max_result_rows: int = Field(default=5_000, ge=1, le=100_000)
    code_max_result_columns: int = Field(default=50, ge=1, le=500)
    code_max_cell_chars: int = Field(default=1_000, ge=10, le=20_000)
    code_max_payload_bytes: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    code_sample_rows: int = Field(default=5, ge=1, le=20)
    code_expose_generated_code: bool = True

    # 查询结果 Excel 导出：文件由 Python 落盘，前端只能经 Java 网关下载。
    report_export_enabled: bool = True
    report_export_dir: Path = Path(".runtime/exports")
    report_export_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    report_export_max_files: int = Field(default=100, ge=1, le=1_000)
    report_export_max_bytes: int = Field(
        default=20_000_000,
        ge=100_000,
        le=100_000_000,
    )

    # Word 可视化报告：与 Excel 共用导出目录，额外调用一次模型生成结论。
    word_report_enabled: bool = True
    report_summary_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    report_word_max_rows: int = Field(default=50, ge=1, le=200)
    report_word_max_columns: int = Field(default=8, ge=1, le=20)
    report_chart_max_categories: int = Field(default=12, ge=3, le=50)

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

    @field_validator("code_sandbox_mode")
    @classmethod
    def validate_code_sandbox_mode(cls, value: str) -> str:
        """沙箱模式只允许明确支持的取值，避免误配置后退化成不安全执行。"""
        normalized = value.strip().lower()
        if normalized not in {"bwrap", "disabled"}:
            raise ValueError("code_sandbox_mode 只支持 bwrap 或 disabled")
        return normalized

    @field_validator("default_llm_provider")
    @classmethod
    def normalize_default_llm_provider(cls, value: str) -> str:
        """默认 Provider 必须是已接入的 OpenAI 兼容服务。"""
        normalized = value.strip().lower() or "deepseek"
        if normalized not in {"deepseek", "doubao", "qwen"}:
            raise ValueError("default_llm_provider 只支持 deepseek / doubao / qwen")
        return normalized

    @field_validator(
        "rpa_download_dir",
        "data_table_dir",
        "report_export_dir",
        mode="after",
    )
    @classmethod
    def anchor_paths(cls, value: Path) -> Path:
        """相对路径统一挂到 backend/ 下，消除对进程工作目录的依赖。"""
        return _anchor_to_app_home(value)

    @property
    def deepseek_key(self) -> str:
        """返回明文 Key，仅在构造 HTTP 请求头时使用。"""
        return self.deepseek_api_key.get_secret_value().strip()

    @property
    def cors_origin_list(self) -> list[str]:
        """把环境变量中的逗号分隔白名单转成列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
