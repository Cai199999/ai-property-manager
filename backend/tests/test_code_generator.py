import asyncio
import json

import httpx
import pandas as pd
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import LLMServiceError
from app.schemas.assistant import TimeRange
from app.services.code_generator import (
    DeepSeekCodeGenerator,
    build_initial_messages,
    build_table_profile,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "工单编号": ["T-123456", "T-654321"],
            "项目名称": ["望京国际研发园", "望京生命科学园"],
            "负责人": ["张三", "李四"],
            "费用金额": [12.5, 8.0],
        }
    )


def _profile():
    return build_table_profile(
        _frame(),
        time_range=TimeRange(start_date="2026-09-01", end_date="2026-09-30"),
        data_source="rpa",
        sample_rows=2,
    )


def test_profile_contains_schema_and_masks_identifiers() -> None:
    profile = _profile()
    prompt = json.dumps(profile.as_prompt_dict(), ensure_ascii=False)

    assert "工单编号" in prompt
    assert "费用金额" in prompt
    assert "T-123456" not in prompt
    assert "张三" not in prompt
    assert profile.row_count == 2


def test_initial_messages_include_profile_and_security_rules() -> None:
    messages = build_initial_messages("按项目统计工单量", _profile())

    assert messages[0]["role"] == "system"
    assert "result_df" in messages[0]["content"]
    assert "禁止 import" in messages[0]["content"]
    assert "项目名称" in messages[1]["content"]
    assert "按项目统计工单量" in messages[1]["content"]


def test_deepseek_code_generator_returns_raw_code() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "```python\nresult_df = source_df.copy()\n```"}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("unit-test-key"),
        deepseek_model="deepseek-chat",
    )
    generator = DeepSeekCodeGenerator(settings, client=client)

    result = asyncio.run(
        generator.complete(
            build_initial_messages("看看数据", _profile()),
            timeout=10,
        )
    )
    asyncio.run(client.aclose())

    assert "result_df" in result
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0.0
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"][0]["role"] == "system"


def test_code_generator_requires_api_key() -> None:
    generator = DeepSeekCodeGenerator(Settings(_env_file=None, deepseek_api_key=SecretStr("")))

    with pytest.raises(LLMServiceError):
        asyncio.run(generator.complete([{"role": "user", "content": "test"}], timeout=1))
    asyncio.run(generator.aclose())
