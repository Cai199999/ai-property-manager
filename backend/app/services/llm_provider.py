"""多模型 Provider 抽象与 BYOK 凭据解析。

当前接入的 DeepSeek、豆包（火山方舟）、通义千问（DashScope）都提供
OpenAI 兼容的 `/chat/completions` 协议，因此这里只做三件事：

1. 按 ``provider_type`` 解析出 base_url、默认模型和系统兜底 Key；
2. 用户传了 ``user_api_key`` 时优先使用用户 Key（Bring Your Own Key）；
3. 统一发起 Chat Completions 请求，并把上游错误映射成明确的业务异常。

安全约束：Key 只保存在 ``SecretStr`` 中，任何日志只能记录
``provider/model/key_source``，禁止记录请求头、请求体或上游响应体。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Sequence

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import (
    LLMAuthError,
    LLMProviderUnsupportedError,
    LLMRateLimitError,
    LLMRequestRejectedError,
    LLMServiceError,
)

logger = logging.getLogger(__name__)

ProviderType = Literal["deepseek", "doubao", "qwen"]
KeySource = Literal["user", "system", "missing"]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """单个 Provider 的静态描述，不包含任何密钥值。"""

    provider_type: str
    label: str
    base_url_attr: str
    model_attr: str
    key_attr: str
    default_base_url: str
    default_model: str


_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        provider_type="deepseek",
        label="DeepSeek",
        base_url_attr="deepseek_base_url",
        model_attr="deepseek_model",
        key_attr="deepseek_api_key",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    ),
    "doubao": ProviderSpec(
        provider_type="doubao",
        label="豆包 / 火山方舟",
        base_url_attr="doubao_base_url",
        model_attr="doubao_model",
        key_attr="doubao_api_key",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-1-6-250615",
    ),
    "qwen": ProviderSpec(
        provider_type="qwen",
        label="通义千问 / DashScope",
        base_url_attr="qwen_base_url",
        model_attr="qwen_model",
        key_attr="qwen_api_key",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
    ),
}

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(_PROVIDER_SPECS)


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """一次请求最终使用的 Provider 配置。

    ``api_key`` 使用 SecretStr，dataclass 的 repr 只会显示 ``**********``。
    ``describe()`` 是唯一允许进入日志或响应的元信息入口。
    """

    provider_type: str
    model: str
    endpoint: str
    api_key: SecretStr
    key_source: KeySource

    @property
    def has_key(self) -> bool:
        return bool(self.api_key.get_secret_value().strip())

    def describe(self) -> dict[str, str]:
        """返回可安全展示的元信息，绝不含 Key 片段。"""
        return {
            "provider_type": self.provider_type,
            "model": self.model,
            "key_source": self.key_source,
        }


def _secret_text(value: str | SecretStr | None) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip()
    return str(value).strip()


def _setting_text(settings: Settings, attr: str) -> str:
    return str(getattr(settings, attr, "") or "").strip()


def _setting_secret(settings: Settings, attr: str) -> str:
    value = getattr(settings, attr, None)
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip()
    return str(value or "").strip()


def _normalize_provider_type(value: str | None, default: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized:
        return normalized
    return (default or "deepseek").strip().lower() or "deepseek"


class LLMProviderFactory:
    """Provider 工厂与统一 Chat Completions 客户端。

    工厂只持有 HTTP 连接池，不持有业务状态，可安全地在请求间共享。
    httpx.AsyncClient 采用懒加载，测试或纯本地降级场景不会创建连接。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def resolve(
        self,
        *,
        provider_type: str | None = None,
        user_api_key: str | SecretStr | None = None,
        model_name: str | None = None,
    ) -> ResolvedProvider:
        """解析本次请求的 Provider；用户 Key 优先于系统 Key。"""
        normalized = _normalize_provider_type(
            provider_type,
            self._settings.default_llm_provider,
        )
        spec = _PROVIDER_SPECS.get(normalized)
        if spec is None:
            raise LLMProviderUnsupportedError(
                details={
                    "provider_type": normalized[:64],
                    "supported": list(SUPPORTED_PROVIDERS),
                }
            )

        user_key = _secret_text(user_api_key)
        system_key = _setting_secret(self._settings, spec.key_attr)
        if user_key:
            api_key, key_source = user_key, "user"
        elif system_key:
            api_key, key_source = system_key, "system"
        else:
            # 没有 Key 属于鉴权问题，返回 401 而不是 502；
            # 错误详情只说明哪个 Provider 缺 Key，不回显任何凭据。
            raise LLMAuthError(
                f"未配置 {spec.label} 的 API Key",
                details={"provider_type": normalized, "key_source": "missing"},
            )

        base_url = _setting_text(self._settings, spec.base_url_attr) or spec.default_base_url
        model = (
            (model_name or "").strip()
            or _setting_text(self._settings, spec.model_attr)
            or spec.default_model
        )
        return ResolvedProvider(
            provider_type=normalized,
            model=model,
            endpoint=f"{base_url.rstrip('/')}/chat/completions",
            api_key=SecretStr(api_key),
            key_source=key_source,
        )

    def describe(
        self,
        *,
        provider_type: str | None = None,
        user_api_key: str | SecretStr | None = None,
        model_name: str | None = None,
    ) -> dict[str, str]:
        """返回本次选择的元信息，用于响应体展示；未知 Provider 不抛异常。"""
        normalized = _normalize_provider_type(
            provider_type,
            self._settings.default_llm_provider,
        )
        spec = _PROVIDER_SPECS.get(normalized)
        if spec is None:
            return {
                "provider_type": normalized[:64],
                "model": (model_name or "").strip(),
                "key_source": "missing",
            }

        user_key = _secret_text(user_api_key)
        system_key = _setting_secret(self._settings, spec.key_attr)
        if user_key:
            key_source: KeySource = "user"
        elif system_key:
            key_source = "system"
        else:
            key_source = "missing"

        model = (
            (model_name or "").strip()
            or _setting_text(self._settings, spec.model_attr)
            or spec.default_model
        )
        return {
            "provider_type": normalized,
            "model": model,
            "key_source": key_source,
        }

    async def chat(
        self,
        provider: ResolvedProvider,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1_600,
        timeout: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """调用 OpenAI 兼容的 Chat Completions，返回首条消息文本。

        日志只记录 provider/model/key_source/status，不记录 headers 与 body。
        """
        body: dict[str, Any] = {
            "model": provider.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            body["response_format"] = response_format

        effective_timeout = timeout or self._settings.deepseek_timeout_seconds
        try:
            response = await self._http().post(
                provider.endpoint,
                headers={
                    "Authorization": f"Bearer {provider.api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=effective_timeout,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "大模型请求超时 provider=%s model=%s key_source=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
            )
            raise LLMServiceError("大模型服务请求超时") from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "大模型请求失败 provider=%s model=%s key_source=%s error=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
                type(exc).__name__,
            )
            raise LLMServiceError("大模型服务暂时不可用") from exc

        status = response.status_code
        if status in {401, 403}:
            logger.warning(
                "大模型鉴权失败 provider=%s model=%s key_source=%s status=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
                status,
            )
            raise LLMAuthError(details=provider.describe())
        if status == 429:
            logger.warning(
                "大模型限流 provider=%s model=%s key_source=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
            )
            raise LLMRateLimitError(details=provider.describe())
        if status >= 500:
            logger.warning(
                "大模型上游异常 provider=%s model=%s key_source=%s status=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
                status,
            )
            raise LLMServiceError(details=provider.describe())
        if status >= 400:
            # 常见于 model_name 不存在或消息格式不合法；不回显上游错误体，
            # 避免第三方响应中夹带 Key 尾号等信息。
            logger.warning(
                "大模型拒绝请求 provider=%s model=%s key_source=%s status=%s",
                provider.provider_type,
                provider.model,
                provider.key_source,
                status,
            )
            raise LLMRequestRejectedError(details=provider.describe())

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "大模型响应格式异常 provider=%s model=%s error=%s",
                provider.provider_type,
                provider.model,
                type(exc).__name__,
            )
            raise LLMServiceError("大模型响应格式异常") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMServiceError("大模型未返回有效内容")
        return content
