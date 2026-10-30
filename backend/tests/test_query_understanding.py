import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import LLMAuthError, TimeRangeParseError
from app.services.deepseek_extractor import LLMTimeRangeExtractor
from app.services.llm_provider import LLMProviderFactory
from app.services.query_understanding import LLMQueryUnderstanding


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr("unit-test-key"),
        "llm_fallback_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _client(content: str, captured: dict[str, object] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _understand(content: str, tables: list[str], captured: dict[str, object] | None = None):
    client = _client(content, captured)
    settings = _settings()
    factory = LLMProviderFactory(settings, client=client)
    service = LLMQueryUnderstanding(settings, provider_factory=factory)

    try:
        return asyncio.run(
            service.understand(
                "用户原话",
                table_names=tables,
            )
        )
    finally:
        asyncio.run(client.aclose())


def test_delete_all_intent_is_semantically_recognized() -> None:
    captured: dict[str, object] = {}
    result = _understand(
        '{"intent":"delete_all_tables","intent_confidence":0.99}',
        ["工单表_A区", "工单表_B区"],
        captured,
    )

    assert result.intent == "delete_all_tables"
    assert result.time_extraction is None
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.0
    messages = payload["messages"]
    user_payload = json.loads(messages[1]["content"])
    assert user_payload["tables"] == ["工单表_A区", "工单表_B区"]
    assert "用户原话" in user_payload["message"]


def test_delete_specific_table_accepts_only_catalog_names() -> None:
    matched = _understand(
        '{"intent":"delete_specific_table","table_name":"工单表_A区",' '"intent_confidence":0.96}',
        ["工单表_A区", "工单表_B区"],
    )
    assert matched.intent == "delete_specific_table"
    assert matched.table_name == "工单表_A区"

    # 模型即使输出路径或别名，也不能直接成为删除目标。
    rejected = _understand(
        '{"intent":"delete_specific_table","table_name":"../工单表_A区",'
        '"intent_confidence":0.96}',
        ["工单表_A区", "工单表_B区"],
    )
    assert rejected.intent == "ambiguous_delete"
    assert rejected.table_name == "../工单表_A区"


def test_low_confidence_delete_degrades_to_ambiguous() -> None:
    result = _understand(
        '{"intent":"delete_all_tables","intent_confidence":0.3}',
        ["工单表_A区"],
    )

    assert result.intent == "ambiguous_delete"
    assert "不够确定" in result.reason


def test_query_intent_keeps_time_range_contract() -> None:
    result = _understand(
        '{"intent":"query","start_date":"2026-09-01","end_date":"2026-09-30",'
        '"confidence":0.97,"reason":"上个月"}',
        [],
    )

    assert result.intent == "query"
    assert result.time_extraction is not None
    assert result.time_extraction.time_range.start_date.isoformat() == "2026-09-01"
    assert result.time_extraction.engine == "deepseek"


def test_missing_time_for_query_raises_time_range_error() -> None:
    with pytest.raises(TimeRangeParseError):
        _understand(
            '{"intent":"query","error":"NO_TIME_RANGE","confidence":0.0}',
            [],
        )


def test_legacy_date_json_without_intent_defaults_to_query() -> None:
    result = _understand(
        '{"start_date":"2026-09-01","end_date":"2026-09-30","confidence":0.9}',
        [],
    )

    assert result.intent == "query"
    assert result.time_extraction is not None


def test_user_key_auth_failure_does_not_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(llm_fallback_enabled=True)
    factory = LLMProviderFactory(settings, client=client)
    service = LLMQueryUnderstanding(settings, provider_factory=factory)
    provider = factory.resolve(user_api_key="sk-user-invalid-123456")

    try:
        with pytest.raises(LLMAuthError):
            asyncio.run(
                service.understand(
                    "删除全部表",
                    table_names=["工单表_A区"],
                    provider=provider,
                )
            )
    finally:
        asyncio.run(client.aclose())


def test_missing_system_key_falls_back_to_rule_time_parser() -> None:
    settings = _settings(deepseek_api_key=SecretStr(""), llm_fallback_enabled=True)
    service = LLMQueryUnderstanding(settings)

    result = asyncio.run(service.understand("最近7天", table_names=[]))

    assert result.intent == "query"
    assert result.time_extraction is not None
    assert result.time_extraction.engine == "rule_based"
    assert result.warnings


def test_old_extractor_still_works_for_compatibility() -> None:
    """既有注入入口走适配器，但不会产生删除意图。"""
    client = _client('{"start_date":"2026-09-01","end_date":"2026-09-30","confidence":0.9}')
    settings = _settings()
    factory = LLMProviderFactory(settings, client=client)
    extractor = LLMTimeRangeExtractor(settings, provider_factory=factory)

    try:
        result = asyncio.run(extractor.extract("上个月"))
    finally:
        asyncio.run(client.aclose())

    assert result.time_range.start_date.isoformat() == "2026-09-01"
