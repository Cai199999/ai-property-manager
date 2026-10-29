import asyncio
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import LLMServiceError
from app.schemas.assistant import TimeRange
from app.services.code_generator import (
    DeepSeekCodeGenerator,
    build_catalog_profile,
    build_initial_messages,
    build_table_profile,
)
from app.services.mock_tables import generate_mock_tables
from app.services.table_catalog import TableCatalog


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
                    {
                        "message": {
                            "content": "```python\nresult_df = source_df.copy()\n```"
                        }
                    }
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
    generator = DeepSeekCodeGenerator(
        Settings(_env_file=None, deepseek_api_key=SecretStr(""))
    )

    with pytest.raises(LLMServiceError):
        asyncio.run(
            generator.complete([{"role": "user", "content": "test"}], timeout=1)
        )
    asyncio.run(generator.aclose())


def test_multi_table_prompt_contains_relationships_and_merge_rules(
    tmp_path: Path,
) -> None:
    generate_mock_tables(tmp_path)
    snapshot = TableCatalog(Settings(_env_file=None, data_table_dir=tmp_path)).scan()
    time_range = TimeRange(start_date="2026-09-01", end_date="2026-09-30")
    frames = {table.name: table.frame for table in snapshot.tables}
    profile = build_catalog_profile(
        snapshot.tables,
        frames=frames,
        time_range=time_range,
        data_source="directory",
        primary_table=snapshot.primary_table or "",
        time_filtered=True,
        relationships=snapshot.relationships,
        sample_rows=2,
    )

    messages = build_initial_messages("按项目区域统计工单量", profile)
    prompt = messages[1]["content"]

    assert "catalog_profile" in prompt
    assert "工单表" in prompt
    assert "项目表" in prompt
    assert "项目编号" in prompt
    assert "pd.merge" in messages[0]["content"]
    assert "manifest" in messages[1]["content"]
    assert "T-" not in prompt
    # 单号等直接标识在样例中必须脱敏。
    assert "202609" not in prompt
