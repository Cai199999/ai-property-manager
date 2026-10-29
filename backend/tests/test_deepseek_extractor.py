import asyncio
import json

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.services.deepseek_extractor import DeepSeekTimeRangeExtractor


def test_deepseek_extracts_time_range_from_json_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"start_date": "2026-09-01", "end_date": "2026-09-30",'
                                ' "confidence": 0.97, "reason": "上个月"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("unit-test-key"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        llm_fallback_enabled=False,
    )
    extractor = DeepSeekTimeRangeExtractor(settings, client=client)

    result = asyncio.run(extractor.extract("汇总上个月的工单"))
    asyncio.run(client.aclose())

    assert result.engine == "deepseek"
    assert result.time_range.start_date.isoformat() == "2026-09-01"
    assert result.time_range.end_date.isoformat() == "2026-09-30"
    assert result.model == "deepseek-chat"
    assert captured["authorization"] == "Bearer unit-test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"] == {"type": "json_object"}


def test_deepseek_failure_falls_back_to_local_rules() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "temporary"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("unit-test-key"),
        llm_fallback_enabled=True,
    )
    extractor = DeepSeekTimeRangeExtractor(settings, client=client)

    result = asyncio.run(extractor.extract("最近7天"))
    asyncio.run(client.aclose())

    assert result.engine == "rule_based"
    assert result.warnings
