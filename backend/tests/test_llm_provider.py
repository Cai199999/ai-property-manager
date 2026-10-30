"""BYOK Provider 抽象、错误映射与日志脱敏测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapters.base import FetchRequest, FetchResult
from app.config import Settings
from app.core.exceptions import (
    LLMAuthError,
    LLMProviderUnsupportedError,
    LLMRequestRejectedError,
    LLMServiceError,
)
from app.core.logging import SecretRedactionFilter, redact_secrets
from app.main import create_app
from app.schemas.assistant import TimeRange
from app.services.code_executor import CodeExecutionResult
from app.services.code_generator import LLMCodeGenerator
from app.services.deepseek_extractor import LLMTimeRangeExtractor
from app.services.llm_provider import LLMProviderFactory
from app.services.time_range import TimeExtraction


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "doubao_api_key": SecretStr(""),
        "qwen_api_key": SecretStr(""),
        "llm_fallback_enabled": False,
        "table_directory_enabled": False,
        "code_query_enabled": True,
        "report_export_enabled": False,
        "word_report_enabled": False,
        "max_query_days": 366,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _mock_client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_user_key_takes_precedence_over_system_key() -> None:
    settings = _settings(
        deepseek_api_key=SecretStr("system-key"),
        deepseek_model="deepseek-chat",
    )
    factory = LLMProviderFactory(settings)

    provider = factory.resolve(user_api_key="sk-user-key-12345678")

    assert provider.provider_type == "deepseek"
    assert provider.key_source == "user"
    assert provider.api_key.get_secret_value() == "sk-user-key-12345678"
    assert provider.model == "deepseek-chat"
    assert provider.endpoint == "https://api.deepseek.com/chat/completions"
    # describe 只能输出元信息，不能包含 Key。
    assert provider.describe() == {
        "provider_type": "deepseek",
        "model": "deepseek-chat",
        "key_source": "user",
    }


def test_system_key_is_used_when_user_key_absent() -> None:
    settings = _settings(deepseek_api_key=SecretStr("system-key"))
    provider = LLMProviderFactory(settings).resolve()

    assert provider.key_source == "system"
    assert provider.api_key.get_secret_value() == "system-key"


def test_provider_and_model_overrides_are_resolved_per_type() -> None:
    settings = _settings(
        qwen_api_key=SecretStr("qwen-system-key"),
        qwen_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/",
    )
    provider = LLMProviderFactory(settings).resolve(
        provider_type=" QWEN ",
        model_name="qwen-max",
    )

    assert provider.provider_type == "qwen"
    assert provider.model == "qwen-max"
    assert provider.endpoint == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )


def test_unknown_provider_returns_400_not_500() -> None:
    factory = LLMProviderFactory(_settings())

    with pytest.raises(LLMProviderUnsupportedError) as excinfo:
        factory.resolve(provider_type="fake-llm", user_api_key="sk-fake-12345678")

    assert excinfo.value.status_code == 400
    assert excinfo.value.details["supported"] == ["deepseek", "doubao", "qwen"]


def test_missing_key_returns_401() -> None:
    factory = LLMProviderFactory(_settings())

    with pytest.raises(LLMAuthError) as excinfo:
        factory.resolve(provider_type="doubao")

    assert excinfo.value.status_code == 401
    assert excinfo.value.details == {
        "provider_type": "doubao",
        "key_source": "missing",
    }


def test_chat_maps_upstream_401_to_auth_error() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = _mock_client(handler)
    factory = LLMProviderFactory(_settings(), client=client)
    provider = factory.resolve(provider_type="doubao", user_api_key="sk-user-fake-1234")

    with pytest.raises(LLMAuthError) as excinfo:
        asyncio.run(factory.chat(provider, [{"role": "user", "content": "hi"}]))
    asyncio.run(client.aclose())

    assert excinfo.value.status_code == 401
    assert captured["authorization"] == "Bearer sk-user-fake-1234"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (429, "RATE"),
        (500, LLMServiceError),
        (400, LLMRequestRejectedError),
    ],
)
def test_chat_maps_upstream_errors(status: int, error_type: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    client = _mock_client(handler)
    factory = LLMProviderFactory(_settings(), client=client)
    provider = factory.resolve(user_api_key="sk-user-key-1234")

    with pytest.raises(LLMServiceError) as excinfo:
        asyncio.run(factory.chat(provider, [{"role": "user", "content": "hi"}]))
    asyncio.run(client.aclose())

    if error_type == "RATE":
        assert excinfo.value.code == "LLM_RATE_LIMITED"
        assert excinfo.value.status_code == 429
    else:
        assert isinstance(excinfo.value, error_type)


def test_user_key_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = _mock_client(handler)
    factory = LLMProviderFactory(_settings(), client=client)
    provider = factory.resolve(user_api_key="sk-super-secret-1234567890")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMAuthError):
            asyncio.run(factory.chat(provider, [{"role": "user", "content": "hi"}]))
    asyncio.run(client.aclose())

    assert "sk-super-secret-1234567890" not in caplog.text
    assert "key_source=user" in caplog.text


def test_redaction_filter_masks_keys_in_log_records() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="调用失败 key=%s header=%s",
        args=("sk-super-secret-1234567890", "Bearer abcdefgh12345678"),
        exc_info=None,
    )

    SecretRedactionFilter().filter(record)

    message = record.getMessage()
    assert "sk-super-secret" not in message
    assert "abcdefgh12345678" not in message
    assert "sk-***" in message
    assert redact_secrets("user_api_key=sk-1234567890") == "user_api_key=***"


def test_extractor_does_not_fallback_when_user_key_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = _mock_client(handler)
    settings = _settings(llm_fallback_enabled=True)
    factory = LLMProviderFactory(settings, client=client)
    extractor = LLMTimeRangeExtractor(settings, provider_factory=factory)
    provider = factory.resolve(user_api_key="sk-user-fake-1234")

    with pytest.raises(LLMAuthError):
        asyncio.run(extractor.extract("最近7天", provider=provider))
    asyncio.run(client.aclose())


# ── API 级集成：验证 401/400 不会退化成 500 ────────────────────────────────


class _FakeFetcher:
    source = "fake"

    async def fetch(self, request: FetchRequest) -> FetchResult:
        return FetchResult(
            frame=pd.DataFrame({"项目名称": ["A"], "费用金额": [1.0]}),
            source=self.source,
            fetched_at=datetime.now(timezone.utc),
            metadata={"source_file": "fake.xlsx"},
        )


class _FakeGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, timeout: float, provider=None) -> str:  # type: ignore[no-untyped-def]
        self.calls += 1
        return "result_df = source_df.copy()"

    async def aclose(self) -> None:
        return None


class _FakeExecutor:
    available = True

    def execute(self, source_df, code, *, timeout: float, tables=None, primary_table=None):  # type: ignore[no-untyped-def]
        return CodeExecutionResult(
            columns=[{"name": "项目名称", "dtype": "string"}],
            rows=[{"项目名称": "A"}],
            row_count=1,
        )


class _FakeExtractor:
    async def extract(self, message: str, *, provider=None) -> TimeExtraction:  # type: ignore[no-untyped-def]
        return TimeExtraction(
            time_range=TimeRange(start_date="2026-09-01", end_date="2026-09-30"),
            engine="rule_based",
            model=None,
            confidence=1.0,
            reason="test",
        )

    async def aclose(self) -> None:
        return None


def _app(settings: Settings, factory: LLMProviderFactory | None = None, extractor=None, generator=None):  # type: ignore[no-untyped-def]
    return create_app(
        settings,
        extractor=extractor or _FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=generator or _FakeGenerator(),  # type: ignore[arg-type]
        code_executor=_FakeExecutor(),  # type: ignore[arg-type]
        provider_factory=factory,
    )


def test_api_rejects_unknown_provider_with_400() -> None:
    with TestClient(_app(_settings())) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={
                "message": "统计9月各项目工单量",
                "provider_type": "fake-llm",
                "user_api_key": "sk-fake-key-1234567890",
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "LLM_PROVIDER_UNSUPPORTED"
    assert "sk-fake-key" not in response.text


def test_api_returns_401_when_provider_has_no_key() -> None:
    with TestClient(_app(_settings())) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "统计9月各项目工单量", "provider_type": "qwen"},
        )

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "LLM_AUTH_FAILED"
    assert body["error"]["details"]["provider_type"] == "qwen"


def test_api_returns_401_for_invalid_user_key_instead_of_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-user-fake-123456"
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    http_client = _mock_client(handler)
    settings = _settings(llm_fallback_enabled=True)
    factory = LLMProviderFactory(settings, client=http_client)
    extractor = LLMTimeRangeExtractor(settings, provider_factory=factory)
    app = _app(settings, factory=factory, extractor=extractor)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={
                "message": "统计9月各项目工单量",
                "provider_type": "doubao",
                "user_api_key": "sk-user-fake-123456",
                "model_name": "doubao-test",
            },
        )
    asyncio.run(http_client.aclose())

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "LLM_AUTH_FAILED"
    assert body["error"]["details"]["key_source"] == "user"
    assert "sk-user-fake" not in response.text


def test_success_response_exposes_llm_metadata() -> None:
    with TestClient(_app(_settings(deepseek_api_key=SecretStr("system-key")))) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "统计9月各项目工单量"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["llm"] == {
        "provider_type": "deepseek",
        "model": "deepseek-chat",
        "key_source": "system",
    }
    assert "system-key" not in response.text


@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [
        ("doubao", "doubao-seed-test"),
        ("qwen", "qwen-max-test"),
    ],
)
def test_api_accepts_doubao_and_qwen_success_responses(
    provider_type: str,
    model_name: str,
) -> None:
    """真实 Provider 成功后，parser.engine 等字段必须通过响应校验。

    覆盖 P0：ParserInfo.engine 曾只允许 deepseek/rule_based，
    豆包/千问成功返回时会在 FastAPI 响应序列化阶段报 500。
    """
    captured_models: list[str] = []
    user_key = f"sk-{provider_type}-user-123456"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_models.append(body["model"])
        assert request.headers["Authorization"] == f"Bearer {user_key}"

        if "response_format" in body:
            # 第一次调用：时间解析。
            content = json.dumps(
                {
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                    "confidence": 0.95,
                    "reason": f"{provider_type} mock 解析",
                }
            )
        else:
            # 第二次调用：生成 Pandas 代码。
            content = "result_df = source_df.copy()"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    http_client = _mock_client(handler)
    settings = _settings()
    factory = LLMProviderFactory(settings, client=http_client)
    extractor = LLMTimeRangeExtractor(settings, provider_factory=factory)
    generator = LLMCodeGenerator(settings, provider_factory=factory)
    app = _app(
        settings,
        factory=factory,
        extractor=extractor,
        generator=generator,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={
                "message": "统计9月各项目工单量",
                "provider_type": provider_type,
                "user_api_key": user_key,
                "model_name": model_name,
            },
        )
    asyncio.run(http_client.aclose())

    assert response.status_code == 200, response.text
    body = response.json()
    # 时间解析与代码生成都复用同一个 Provider 与模型。
    assert captured_models == [model_name, model_name]
    assert body["parser"]["engine"] == provider_type
    assert body["parser"]["model"] == model_name
    assert body["meta"]["llm"] == {
        "provider_type": provider_type,
        "model": model_name,
        "key_source": "user",
    }
    assert user_key not in response.text
