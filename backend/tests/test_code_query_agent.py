import asyncio
from datetime import datetime, timezone

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
)
from app.main import create_app
from app.schemas.assistant import TimeRange
from app.services.code_executor import (
    CodeExecutionResult,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from app.services.code_query_agent import CodeQueryAgent
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

    def execute(
        self,
        source_df: pd.DataFrame,
        code: str,
        *,
        timeout: float,
    ) -> CodeExecutionResult:
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
        "code_max_retries": 2,
        "code_total_timeout_seconds": 30.0,
        "code_llm_timeout_seconds": 5.0,
        "code_exec_timeout_seconds": 2.0,
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
