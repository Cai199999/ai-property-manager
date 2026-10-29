import asyncio
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapters.base import FetchRequest, FetchResult
from app.config import Settings
from app.core.exceptions import (
    CodeExecutionTimeoutError,
    CodeGenerationError,
    CodeSandboxUnavailableError,
    TimeRangeParseError,
)
from app.main import create_app
from app.schemas.assistant import TimeRange
from app.services.code_executor import (
    CodeExecutionResult,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from app.services.code_query_agent import CodeQueryAgent
from app.services.mock_tables import generate_mock_tables
from app.services.table_catalog import TableCatalog
from app.services.time_range import TimeExtraction


class _FakeExtractor:
    async def extract(self, message: str) -> TimeExtraction:
        return TimeExtraction(
            time_range=TimeRange(start_date="2026-09-01", end_date="2026-09-30"),
            engine="rule_based",
            model=None,
            confidence=1.0,
            reason="test",
        )

    async def aclose(self) -> None:
        return None


class _FakeFetcher:
    source = "fake"

    async def fetch(self, request: FetchRequest) -> FetchResult:
        return FetchResult(
            frame=pd.DataFrame(
                {
                    "工单编号": ["T-1", "T-2"],
                    "项目名称": ["A", "B"],
                    "费用金额": [10.0, 20.0],
                }
            ),
            source=self.source,
            fetched_at=datetime.now(timezone.utc),
            metadata={"source_file": "fake.xlsx"},
        )


class _FakeGenerator:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("没有更多假响应")
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None


class _FakeExecutor:
    available = True

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.frames: dict[str, pd.DataFrame] | None = None

    def execute(
        self,
        source_df: pd.DataFrame,
        code: str,
        *,
        timeout: float,
        tables: dict[str, pd.DataFrame] | None = None,
        primary_table: str | None = None,
    ) -> CodeExecutionResult:
        self.frames = tables
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, CodeExecutionResult)
        return outcome


def _success() -> CodeExecutionResult:
    return CodeExecutionResult(
        columns=[{"name": "项目名称", "dtype": "string"}],
        rows=[{"项目名称": "A"}],
        row_count=1,
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "code_query_enabled": True,
        "table_directory_enabled": False,
        "code_max_retries": 2,
        "code_total_timeout_seconds": 30.0,
        "code_llm_timeout_seconds": 5.0,
        "code_exec_timeout_seconds": 2.0,
        "report_export_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _agent(generator: _FakeGenerator, executor: _FakeExecutor) -> CodeQueryAgent:
    return CodeQueryAgent(
        _settings(),
        _FakeExtractor(),  # type: ignore[arg-type]
        _FakeFetcher(),  # type: ignore[arg-type]
        generator,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
    )


def test_agent_returns_generic_result_and_attempts() -> None:
    generator = _FakeGenerator(["```python\nresult_df = source_df.copy()\n```"])
    executor = _FakeExecutor([_success()])

    response = asyncio.run(
        _agent(generator, executor).handle(
            message="按项目统计工单",
            request_id="code-test-001",
        )
    )

    assert response.request_id == "code-test-001"
    assert response.result.row_count == 1
    assert response.code.attempts == 1
    assert response.code.generated_code == "result_df = source_df.copy()"
    assert response.meta.source_file == "fake.xlsx"


class _ExplodingFetcher:
    source = "fake"

    async def fetch(self, request: FetchRequest) -> FetchResult:
        raise AssertionError("目录模式下不应调用 RPA 抓取")


CONCAT_CODE = (
    'group = ["工单表_A区", "工单表_B区", "工单表_C区"]\n'
    "orders = pd.concat([tables[name] for name in group], ignore_index=True)\n"
    "result_df = pd.DataFrame([{\n"
    '    "总工单量": int(orders["单号"].nunique()),\n'
    '    "总金额": round(float(\n'
    '        pd.to_numeric(orders["费用金额"], errors="coerce").sum()\n'
    "    ), 2),\n"
    "}])"
)


def test_agent_uses_directory_tables_before_rpa(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path)
    settings = _settings(
        table_directory_enabled=True,
        data_table_dir=tmp_path,
    )
    generator = _FakeGenerator([CONCAT_CODE])
    executor = _FakeExecutor([_success()])
    catalog = TableCatalog(settings)
    agent = CodeQueryAgent(
        settings,
        _FakeExtractor(),  # type: ignore[arg-type]
        _ExplodingFetcher(),  # type: ignore[arg-type]
        generator,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        table_catalog=catalog,
    )

    response = asyncio.run(
        agent.handle(
            message="汇总所有区域的总工单量和总金额",
            request_id="code-test-directory",
        )
    )

    expected_names = {"工单表_A区", "工单表_B区", "工单表_C区"}
    assert response.data_source == "directory"
    assert response.meta.primary_table == "工单表_A区"
    assert {table.name for table in response.meta.tables} == expected_names
    assert response.meta.source_row_count == summary["total_work_orders"]
    assert executor.frames is not None
    assert set(executor.frames) == expected_names
    for name in expected_names:
        assert len(executor.frames[name]) == summary["tables"][name]
        assert "费用金额" in executor.frames[name].columns
    prompt = generator.calls[0][1]["content"]
    assert "catalog_profile" in prompt
    assert "homogeneous_groups" in prompt
    assert "工单表_A区" in prompt


class _ExplodingExtractor:
    async def extract(self, message: str) -> TimeExtraction:
        raise TimeRangeParseError("未识别到时间范围，请补充时间")

    async def aclose(self) -> None:
        return None


def test_agent_defaults_to_full_directory_range_without_time(
    tmp_path: Path,
) -> None:
    """用户只说“汇总所有区域”时，默认使用数据文件覆盖的完整区间。"""
    summary = generate_mock_tables(tmp_path)
    settings = _settings(
        table_directory_enabled=True,
        data_table_dir=tmp_path,
    )
    generator = _FakeGenerator([CONCAT_CODE])
    executor = _FakeExecutor([_success()])
    agent = CodeQueryAgent(
        settings,
        _ExplodingExtractor(),  # type: ignore[arg-type]
        _ExplodingFetcher(),  # type: ignore[arg-type]
        generator,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        table_catalog=TableCatalog(settings),
    )

    response = asyncio.run(
        agent.handle(
            message="汇总所有区域的总工单量和总金额",
            request_id="code-test-default-range",
        )
    )

    assert response.time_range.start_date.isoformat() == "2026-09-01"
    assert response.time_range.end_date.isoformat() == "2026-09-30"
    assert response.meta.source_row_count == summary["total_work_orders"]
    assert any("默认汇总" in warning for warning in response.meta.warnings)


def test_agent_repairs_static_validation_error() -> None:
    generator = _FakeGenerator(
        [
            'result_df = source_df.to_csv("/tmp/x")',
            "result_df = source_df.copy()",
        ]
    )
    executor = _FakeExecutor([_success()])

    response = asyncio.run(
        _agent(generator, executor).handle(
            message="按项目统计工单",
            request_id="code-test-002",
        )
    )

    assert response.code.attempts == 2
    assert "to_csv" in response.code.retry_errors[0]
    assert [message["role"] for message in generator.calls[1]] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_agent_repairs_execution_error() -> None:
    generator = _FakeGenerator(
        [
            "result_df = source_df.copy()",
            "result_df = source_df.copy()",
        ]
    )
    executor = _FakeExecutor([SandboxExecutionError("列名不存在"), _success()])

    response = asyncio.run(
        _agent(generator, executor).handle(
            message="按项目统计工单",
            request_id="code-test-003",
        )
    )

    assert response.code.attempts == 2
    assert "列名不存在" in response.code.retry_errors[0]


def test_agent_fails_after_two_retries() -> None:
    generator = _FakeGenerator(
        [
            "result_df = source_df.to_csv('/tmp/1')",
            "result_df = source_df.to_csv('/tmp/2')",
            "result_df = source_df.to_csv('/tmp/3')",
        ]
    )
    executor = _FakeExecutor([])

    with pytest.raises(CodeGenerationError) as error:
        asyncio.run(
            _agent(generator, executor).handle(
                message="按项目统计工单",
                request_id="code-test-004",
            )
        )

    assert error.value.details["attempts"] == 3


def test_agent_returns_timeout_after_retry_budget() -> None:
    generator = _FakeGenerator(
        [
            "result_df = source_df.copy()",
            "result_df = source_df.copy()",
            "result_df = source_df.copy()",
        ]
    )
    executor = _FakeExecutor(
        [
            SandboxTimeoutError("超时"),
            SandboxTimeoutError("超时"),
            SandboxTimeoutError("超时"),
        ]
    )

    with pytest.raises(CodeExecutionTimeoutError):
        asyncio.run(
            _agent(generator, executor).handle(
                message="按项目统计工单",
                request_id="code-test-005",
            )
        )


def test_agent_fails_closed_when_sandbox_unavailable() -> None:
    executor = _FakeExecutor([])
    executor.available = False

    with pytest.raises(CodeSandboxUnavailableError):
        asyncio.run(
            _agent(_FakeGenerator([]), executor).handle(
                message="按项目统计工单",
                request_id="code-test-006",
            )
        )


def test_code_query_api_returns_generic_result() -> None:
    generator = _FakeGenerator(["```python\nresult_df = source_df.copy()\n```"])
    executor = _FakeExecutor([_success()])
    app = create_app(
        _settings(),
        extractor=_FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=generator,  # type: ignore[arg-type]
        code_executor=executor,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            headers={"X-Request-Id": "api-code-001"},
            json={"message": "按项目统计工单"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "api-code-001"
    assert body["result"]["row_count"] == 1
    assert body["code"]["attempts"] == 1
    assert response.headers["X-Request-Id"] == "api-code-001"


def test_code_query_api_returns_422_after_retries() -> None:
    generator = _FakeGenerator(
        [
            "result_df = source_df.to_csv('/tmp/1')",
            "result_df = source_df.to_csv('/tmp/2')",
            "result_df = source_df.to_csv('/tmp/3')",
        ]
    )
    app = create_app(
        _settings(),
        extractor=_FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=generator,  # type: ignore[arg-type]
        code_executor=_FakeExecutor([]),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "按项目统计工单"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CODE_GENERATION_FAILED"


def test_code_query_exports_excel_and_download_endpoint(tmp_path: Path) -> None:
    settings = _settings(
        report_export_enabled=True,
        report_export_dir=tmp_path / "exports",
    )
    generator = _FakeGenerator(["result_df = source_df.copy()"])
    app = create_app(
        settings,
        extractor=_FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=generator,  # type: ignore[arg-type]
        code_executor=_FakeExecutor([_success()]),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "按项目统计工单"},
        )
        body = response.json()
        download_url = body["download_url"]
        download = client.get(download_url)
        invalid = client.get("/api/v1/assistant/download/not-a-report.xlsx")

    assert response.status_code == 200
    assert body["chart_meta"]["type"] == "table"
    assert download_url.startswith("/api/v1/assistant/download/report_")
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "REPORT_NAME_INVALID"


def test_code_query_exports_word_report_and_downloads_docx(tmp_path: Path) -> None:
    """Word 报告与 Excel 同时产出，且都走同一个下载接口。"""
    settings = _settings(
        report_export_enabled=True,
        word_report_enabled=True,
        report_export_dir=tmp_path / "exports",
    )
    app = create_app(
        settings,
        extractor=_FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=_FakeGenerator(["result_df = source_df.copy()"]),  # type: ignore[arg-type]
        code_executor=_FakeExecutor([_success()]),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "按项目统计工单"},
        )
        body = response.json()
        word_url = body["word_download_url"]
        download = client.get(word_url)

    assert response.status_code == 200
    assert body["download_url"].endswith(".xlsx")
    assert word_url.startswith("/api/v1/assistant/download/report_")
    assert word_url.endswith(".docx")
    # 未配置 DeepSeek Key 时属于正常降级，不应提示“AI 总结失败”
    assert not any("AI 总结生成失败" in warning for warning in body["meta"]["warnings"])

    assert download.status_code == 200
    assert download.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert download.content[:2] == b"PK"

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "word/document.xml" in archive.namelist()


class _ExplodingWordExporter:
    """模拟 Word 生成异常，验证降级不影响主查询。"""

    enabled = True

    def export(self, **_: object) -> None:
        raise RuntimeError("docx 生成失败")


def test_word_export_failure_degrades_to_warning(tmp_path: Path) -> None:
    settings = _settings(
        report_export_enabled=True,
        word_report_enabled=True,
        report_export_dir=tmp_path / "exports",
    )
    app = create_app(
        settings,
        extractor=_FakeExtractor(),  # type: ignore[arg-type]
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        code_generator=_FakeGenerator(["result_df = source_df.copy()"]),  # type: ignore[arg-type]
        code_executor=_FakeExecutor([_success()]),  # type: ignore[arg-type]
    )
    app.state.code_query_agent._word_exporter = _ExplodingWordExporter()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "按项目统计工单"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["word_download_url"] is None
    assert body["download_url"].endswith(".xlsx")
    assert "Word 报告生成失败，可先下载 Excel 结果" in body["meta"]["warnings"]
