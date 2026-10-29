"""生成式 Pandas 查询编排：检索数据 -> 生成代码 -> 沙箱执行 -> 自我修复。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from time import monotonic, perf_counter

import anyio
import pandas as pd

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
    QueryTableInfo,
    ResultColumn,
)
from app.services.code_executor import (
    CodeExecutor,
    SandboxExecutionError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from app.services.code_generator import (
    CatalogProfile,
    DeepSeekCodeGenerator,
    TableProfile,
    build_catalog_profile,
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
from app.services.table_catalog import TableCatalog, TableSnapshot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SourceData:
    """本次查询实际使用的数据快照。"""

    frames: dict[str, pd.DataFrame]
    primary_table: str
    profile: TableProfile | CatalogProfile
    data_source: str
    fetched_at: datetime
    source_row_count: int
    source_file: str | None
    tables: list[QueryTableInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class CodeQueryAgent:
    """无状态代码查询编排器。"""

    def __init__(
        self,
        settings: Settings,
        extractor: DeepSeekTimeRangeExtractor,
        fetcher: DataFetcher,
        generator: DeepSeekCodeGenerator,
        executor: CodeExecutor,
        table_catalog: TableCatalog | None = None,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._fetcher = fetcher
        self._generator = generator
        self._executor = executor
        self._table_catalog = (
            table_catalog
            if table_catalog is not None and settings.table_directory_enabled
            else None
        )

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

        source = await self._resolve_source(
            time_range=extraction.time_range,
            property_name=property_name,
        )
        messages = build_initial_messages(message, source.profile)

        warnings = list(extraction.warnings) + list(source.warnings)
        warnings = _dedupe(warnings)
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
                        source.frames[source.primary_table],
                        code,
                        timeout=min(
                            self._settings.code_exec_timeout_seconds, remaining
                        ),
                        tables=source.frames,
                        primary_table=source.primary_table,
                    )
                )
            except SandboxUnavailableError as exc:
                raise CodeSandboxUnavailableError(
                    details={"reason": sanitize_error_text(exc)}
                ) from exc
            except SandboxTimeoutError as exc:
                error_message = sanitize_error_text(exc)
                retry_errors.append(error_message)
                logger.info(
                    "代码执行超时 request_id=%s attempt=%s", request_id, attempt
                )
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
                data_source=source.data_source,
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
                    fetched_at=source.fetched_at.astimezone(self._settings.tz),
                    elapsed_ms=elapsed_ms,
                    source_row_count=source.source_row_count,
                    source_file=source.source_file,
                    primary_table=source.primary_table,
                    tables=source.tables,
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

    async def _resolve_source(
        self,
        *,
        time_range: TimeRange,
        property_name: str | None,
    ) -> _SourceData:
        """优先使用多表目录；目录为空或扫描失败时回退 RPA 单表。"""
        extra_warnings: list[str] = []
        if self._table_catalog is not None:
            try:
                snapshot = await anyio.to_thread.run_sync(self._table_catalog.scan)
            except Exception as exc:  # noqa: BLE001 - 目录异常不应阻断旧数据源
                logger.warning("数据表目录扫描失败: %s", type(exc).__name__)
                extra_warnings.append("数据表目录扫描失败，已回退到 RPA 数据源")
            else:
                if snapshot.has_tables:
                    return self._directory_source(
                        snapshot,
                        time_range=time_range,
                        property_name=property_name,
                    )

        return await self._rpa_source(
            time_range=time_range,
            property_name=property_name,
            extra_warnings=extra_warnings,
        )

    def _directory_source(
        self,
        snapshot: TableSnapshot,
        *,
        time_range: TimeRange,
        property_name: str | None,
    ) -> _SourceData:
        primary = snapshot.primary_table or snapshot.tables[0].name
        by_name = snapshot.table_map()
        primary_dataset = by_name[primary]
        frames = {table.name: table.frame for table in snapshot.tables}

        filtered, filter_warnings, time_filtered = _filter_primary_frame(
            frames[primary],
            primary_dataset.time_column,
            time_range=time_range,
            property_name=property_name,
        )
        frames[primary] = filtered

        profile = build_catalog_profile(
            snapshot.tables,
            frames=frames,
            time_range=time_range,
            data_source="directory",
            primary_table=primary,
            time_filtered=time_filtered,
            relationships=snapshot.relationships,
            sample_rows=self._settings.code_sample_rows,
        )
        table_infos = [
            QueryTableInfo(
                name=table.name,
                role=table.role,
                row_count=len(frames[table.name]),
                file_name=table.file_name,
            )
            for table in snapshot.tables
        ]

        warnings = list(snapshot.warnings) + filter_warnings
        if filtered.empty:
            warnings.append("主表在所选时间/项目条件下没有数据")

        return _SourceData(
            frames=frames,
            primary_table=primary,
            profile=profile,
            data_source="directory",
            fetched_at=datetime.now(self._settings.tz),
            source_row_count=int(len(filtered)),
            source_file=primary_dataset.file_name,
            tables=table_infos,
            warnings=_dedupe(warnings),
        )

    async def _rpa_source(
        self,
        *,
        time_range: TimeRange,
        property_name: str | None,
        extra_warnings: list[str],
    ) -> _SourceData:
        fetch_result = await self._fetcher.fetch(
            FetchRequest(
                time_range=time_range,
                property_name=property_name,
            )
        )
        frame = fetch_result.frame
        source_file = fetch_result.metadata.get("source_file")
        profile = build_table_profile(
            frame,
            time_range=time_range,
            data_source=fetch_result.source,
            sample_rows=self._settings.code_sample_rows,
            name="source_df",
            role="fact",
            time_column="登记时间" if "登记时间" in frame.columns else None,
        )
        return _SourceData(
            frames={"source_df": frame},
            primary_table="source_df",
            profile=profile,
            data_source=fetch_result.source,
            fetched_at=fetch_result.fetched_at,
            source_row_count=int(len(frame)),
            source_file=source_file,
            tables=[
                QueryTableInfo(
                    name="source_df",
                    role="fact",
                    row_count=int(len(frame)),
                    file_name=source_file,
                )
            ],
            warnings=list(extra_warnings),
        )


def _filter_primary_frame(
    frame: pd.DataFrame,
    time_column: str | None,
    *,
    time_range: TimeRange,
    property_name: str | None,
) -> tuple[pd.DataFrame, list[str], bool]:
    """只过滤主表，维表保持全量，避免错误裁剪项目/人员主数据。"""
    warnings: list[str] = []
    filtered = frame
    time_filtered = False

    if time_column and time_column in filtered.columns:
        parsed = pd.to_datetime(filtered[time_column], errors="coerce")
        if parsed.notna().any():
            start_at = pd.Timestamp(time_range.start_date)
            end_exclusive = pd.Timestamp(time_range.end_date) + pd.Timedelta(days=1)
            mask = (parsed >= start_at) & (parsed < end_exclusive)
            dropped = int((~mask & parsed.notna()).sum())
            filtered = filtered.loc[mask.fillna(False)].copy()
            time_filtered = True
            if dropped:
                warnings.append(f"主表有 {dropped} 行登记时间超出查询范围，已排除")
        else:
            warnings.append(f"主表时间列 {time_column} 无法解析，未按时间过滤")
    elif time_column:
        warnings.append(f"主表时间列 {time_column} 不存在，未按时间过滤")
    else:
        warnings.append("主表未识别到时间列，未按时间过滤")

    if property_name:
        if "项目名称" in filtered.columns:
            project_mask = (
                filtered["项目名称"]
                .astype("string")
                .str.strip()
                .eq(property_name.strip())
                .fillna(False)
            )
            filtered = filtered.loc[project_mask].copy()
        else:
            warnings.append("主表没有项目名称列，已忽略项目过滤条件")

    return filtered.reset_index(drop=True), warnings, time_filtered


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
