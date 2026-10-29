"""生成式 Pandas 查询编排：检索数据 -> 生成代码 -> 沙箱执行 -> 自我修复。"""

from __future__ import annotations

import logging
from functools import partial
from time import monotonic, perf_counter

import anyio

from app.adapters.base import DataFetcher, FetchRequest
from app.config import Settings
from app.core.exceptions import (
    CodeGenerationError,
    CodeExecutionTimeoutError,
    CodeSandboxUnavailableError,
    TimeRangeParseError,
)
from app.schemas.assistant import ParserInfo, TimeRange
from app.schemas.code_query import (
    CodeExecutionInfo,
    CodeQueryMeta,
    CodeQueryResponse,
    CodeResult,
    ResultColumn,
)
from app.services.code_executor import (
    CodeExecutor,
    SandboxExecutionError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from app.services.code_generator import (
    DeepSeekCodeGenerator,
    build_initial_messages,
    build_retry_feedback,
    build_table_profile,
)
from app.services.code_safety import (
    CodeExtractionError,
    UnsafeCodeError,
    extract_python_code,
    sanitize_error_text,
    validate_generated_code,
)
from app.services.deepseek_extractor import DeepSeekTimeRangeExtractor

logger = logging.getLogger(__name__)


class CodeQueryAgent:
    """无状态代码查询编排器。"""

    def __init__(
        self,
        settings: Settings,
        extractor: DeepSeekTimeRangeExtractor,
        fetcher: DataFetcher,
        generator: DeepSeekCodeGenerator,
        executor: CodeExecutor,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._fetcher = fetcher
        self._generator = generator
        self._executor = executor

    async def handle(
        self,
        *,
        message: str,
        request_id: str,
        property_name: str | None = None,
    ) -> CodeQueryResponse:
        if not self._settings.code_query_enabled:
            raise CodeGenerationError(
                "生成式代码查询未启用",
                details={"code_query_enabled": False},
            )
        if not self._executor.available:
            raise CodeSandboxUnavailableError()

        started_at = perf_counter()
        deadline = monotonic() + self._settings.code_total_timeout_seconds

        extraction = await self._extractor.extract(message)
        _validate_range(extraction.time_range, self._settings)

        fetch_result = await self._fetcher.fetch(
            FetchRequest(
                time_range=extraction.time_range,
                property_name=property_name,
            )
        )
        profile = build_table_profile(
            fetch_result.frame,
            time_range=extraction.time_range,
            data_source=fetch_result.source,
            sample_rows=self._settings.code_sample_rows,
        )
        messages = build_initial_messages(message, profile)

        warnings = list(extraction.warnings)
        retry_errors: list[str] = []
        attempts = 0
        last_code: str | None = None
        final_timeout = False
        max_attempts = self._settings.code_max_retries + 1

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            remaining = deadline - monotonic()
            if remaining <= 1.0:
                final_timeout = True
                retry_errors.append("总执行时间预算已耗尽")
                break

            raw_response = await self._generator.complete(
                messages,
                timeout=min(self._settings.code_llm_timeout_seconds, remaining),
            )
            messages.append({"role": "assistant", "content": raw_response})

            try:
                extracted = extract_python_code(
                    raw_response,
                    max_chars=self._settings.code_max_code_chars,
                )
                code = extracted.code
                validate_generated_code(
                    code,
                    max_chars=self._settings.code_max_code_chars,
                )
                last_code = code
                warnings.extend(extracted.warnings)
            except (CodeExtractionError, UnsafeCodeError) as exc:
                error_message = sanitize_error_text(exc)
                retry_errors.append(error_message)
                logger.info(
                    "代码生成校验失败 request_id=%s attempt=%s error=%s",
                    request_id,
                    attempt,
                    error_message,
                )
                if attempt >= max_attempts:
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": build_retry_feedback(error_message, attempt),
                    }
                )
                continue

            remaining = deadline - monotonic()
            if remaining <= 1.0:
                final_timeout = True
                retry_errors.append("总执行时间预算已耗尽")
                break

            try:
                execution = await anyio.to_thread.run_sync(
                    partial(
                        self._executor.execute,
                        fetch_result.frame,
                        code,
                        timeout=min(self._settings.code_exec_timeout_seconds, remaining),
                    )
                )
            except SandboxUnavailableError as exc:
                raise CodeSandboxUnavailableError(
                    details={"reason": sanitize_error_text(exc)}
                ) from exc
            except SandboxTimeoutError as exc:
                error_message = sanitize_error_text(exc)
                retry_errors.append(error_message)
                logger.info("代码执行超时 request_id=%s attempt=%s", request_id, attempt)
                if attempt >= max_attempts:
                    final_timeout = True
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": build_retry_feedback(error_message, attempt),
                    }
                )
                continue
            except (SandboxExecutionError, UnsafeCodeError) as exc:
                error_message = sanitize_error_text(exc)
                retry_errors.append(error_message)
                logger.info(
                    "代码执行失败 request_id=%s attempt=%s error=%s",
                    request_id,
                    attempt,
                    error_message,
                )
                if attempt >= max_attempts:
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": build_retry_feedback(error_message, attempt),
                    }
                )
                continue

            warnings.extend(execution.warnings)
            if execution.row_count == 0:
                warnings.append("生成代码返回了空结果")

            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info(
                "代码查询完成 request_id=%s rows=%s attempts=%s elapsed_ms=%s",
                request_id,
                execution.row_count,
                attempts,
                elapsed_ms,
            )
            return CodeQueryResponse(
                request_id=request_id,
                query=message,
                data_source=fetch_result.source,
                time_range=extraction.time_range,
                parser=ParserInfo(
                    engine=extraction.engine,
                    model=extraction.model,
                    confidence=extraction.confidence,
                    reason=extraction.reason,
                ),
                result=CodeResult(
                    columns=[
                        ResultColumn(name=column["name"], dtype=column["dtype"])
                        for column in execution.columns
                    ],
                    rows=execution.rows,
                    row_count=execution.row_count,
                ),
                code=CodeExecutionInfo(
                    attempts=attempts,
                    generated_code=(
                        last_code if self._settings.code_expose_generated_code else None
                    ),
                    retry_errors=retry_errors,
                ),
                meta=CodeQueryMeta(
                    fetched_at=fetch_result.fetched_at.astimezone(self._settings.tz),
                    elapsed_ms=elapsed_ms,
                    source_row_count=len(fetch_result.frame),
                    source_file=fetch_result.metadata.get("source_file"),
                    warnings=_dedupe(warnings),
                ),
            )

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.warning(
            "代码查询失败 request_id=%s attempts=%s elapsed_ms=%s errors=%s",
            request_id,
            attempts,
            elapsed_ms,
            retry_errors[-2:],
        )
        details = {
            "attempts": attempts,
            "errors": retry_errors[-3:],
            "elapsed_ms": elapsed_ms,
        }
        if final_timeout:
            raise CodeExecutionTimeoutError(
                "生成代码在时间预算内未执行成功",
                details=details,
            )
        raise CodeGenerationError(details=details)


def _validate_range(time_range: TimeRange, settings: Settings) -> None:
    """与普通查询保持同样的最大时间范围约束。"""
    days = (time_range.end_date - time_range.start_date).days + 1
    if days > settings.max_query_days:
        raise TimeRangeParseError(
            f"查询范围过大：{days} 天，最多支持 {settings.max_query_days} 天",
            details={
                "requested_days": days,
                "max_query_days": settings.max_query_days,
            },
        )


def _dedupe(values: list[str]) -> list[str]:
    """保持警告顺序并去重，避免重试过程重复刷屏。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
