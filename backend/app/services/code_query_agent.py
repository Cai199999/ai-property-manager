"""生成式 Pandas 查询编排：检索数据 -> 生成代码 -> 沙箱执行 -> 自我修复。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from time import monotonic, perf_counter

import anyio
import pandas as pd
from pydantic import SecretStr

from app.adapters.base import DataFetcher, FetchRequest
from app.config import Settings
from app.core.exceptions import (
    CodeGenerationError,
    CodeExecutionTimeoutError,
    CodeSandboxUnavailableError,
    TableDeletionError,
    TableImportUnavailableError,
    TableNotFoundError,
    TimeRangeParseError,
)
from app.schemas.assistant import ParserInfo, TimeRange
from app.schemas.code_query import (
    ActionResultResponse,
    ChartMeta,
    CodeExecutionInfo,
    CodeQueryMeta,
    CodeQueryResponse,
    CodeResult,
    ConfirmationRequiredResponse,
    LLMInfo,
    QueryTableInfo,
    ResultColumn,
)
from app.services.code_executor import (
    CodeExecutionResult,
    CodeExecutor,
    SandboxExecutionError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from app.services.code_generator import (
    CatalogProfile,
    LLMCodeGenerator,
    TableProfile,
    build_catalog_profile,
    build_initial_messages,
    build_retry_feedback,
    build_table_profile,
)
from app.services.chart_meta import infer_chart_meta
from app.services.code_safety import (
    CodeExtractionError,
    UnsafeCodeError,
    extract_python_code,
    sanitize_error_text,
    validate_generated_code,
)
from app.services.confirmation_store import DeleteAllConfirmationStore
from app.services.deepseek_extractor import LLMTimeRangeExtractor
from app.services.llm_provider import LLMProviderFactory, ResolvedProvider
from app.services.query_understanding import (
    ExtractorQueryUnderstanding,
    QueryUnderstanding,
    QueryUnderstandingService,
)
from app.services.report_export import REPORT_DOWNLOAD_PATH, ExcelReportExporter
from app.services.table_catalog import TableCatalog, TableSnapshot
from app.services.table_deletion import TableDeletionService
from app.services.time_range import TimeExtraction
from app.services.word_exporter import (
    LLMReportSummarizer,
    ReportSummary,
    WordReportExporter,
    build_fallback_summary,
)

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
        extractor: LLMTimeRangeExtractor,
        fetcher: DataFetcher,
        generator: LLMCodeGenerator,
        executor: CodeExecutor,
        table_catalog: TableCatalog | None = None,
        report_exporter: ExcelReportExporter | None = None,
        word_exporter: WordReportExporter | None = None,
        report_summarizer: LLMReportSummarizer | None = None,
        provider_factory: LLMProviderFactory | None = None,
        understanding: QueryUnderstandingService | None = None,
        confirmation_store: DeleteAllConfirmationStore | None = None,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._fetcher = fetcher
        self._generator = generator
        self._executor = executor
        # Provider 工厂只负责解析 BYOK 参数；httpx 客户端懒加载，
        # 未传 provider 覆盖时不会产生额外连接。
        self._providers = provider_factory or LLMProviderFactory(settings)
        self._table_catalog = (
            table_catalog
            if table_catalog is not None and settings.table_directory_enabled
            else None
        )
        self._report_exporter = report_exporter
        self._word_exporter = word_exporter
        self._report_summarizer = report_summarizer
        self._table_deletion = (
            TableDeletionService(settings, self._table_catalog)
            if self._table_catalog is not None
            else None
        )
        # 未显式注入时使用旧时间解析器适配器，保证既有测试与自定义注入
        # 兼容；生产入口在 main.create_app 中注入 LLMQueryUnderstanding。
        self._understanding = understanding or ExtractorQueryUnderstanding(extractor)
        self._confirmation_store = confirmation_store or DeleteAllConfirmationStore(
            settings.delete_confirmation_ttl_seconds,
            tz=settings.tz,
        )

    async def _table_context(self) -> tuple[TableSnapshot | None, list[str]]:
        """扫描当前表名，供大模型做语义意图分类。"""
        if self._table_catalog is None:
            return None, []
        try:
            snapshot = await anyio.to_thread.run_sync(self._table_catalog.scan)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不阻断普通查询
            logger.warning("意图识别时扫描目录失败: %s", type(exc).__name__)
            return None, []
        return snapshot, [table.name for table in snapshot.tables]

    async def _handle_intent(
        self,
        understanding: QueryUnderstanding,
        snapshot: TableSnapshot | None,
        table_names: list[str],
        request_id: str,
    ) -> CodeQueryResponse | ActionResultResponse | ConfirmationRequiredResponse | None:
        """处理删表意图；query 返回 None 继续原查询流程。"""
        if understanding.intent == "query":
            return None

        if understanding.intent == "ambiguous_delete":
            reason = understanding.reason or "无法确定要删除的数据表，请明确指定完整表名"
            return self._action_failure(
                request_id=request_id,
                message=reason,
                action="none",
                remaining_tables=table_names,
                candidates=table_names,
            )

        if self._table_deletion is None or self._table_catalog is None:
            return self._action_failure(
                request_id=request_id,
                message="多表数据目录未启用，无法删除数据表",
                action=(
                    "delete_all_tables"
                    if understanding.intent == "delete_all_tables"
                    else "delete_table"
                ),
                remaining_tables=table_names,
            )

        if understanding.intent == "delete_all_tables":
            return await self._request_delete_all(request_id)

        table_name = (understanding.table_name or "").strip()
        # 路径形态在查表前直接拦截，模型输出同样不能绕过工作区白名单。
        try:
            safe_name = self._table_deletion.validate_table_name(table_name)
        except TableDeletionError as exc:
            return self._action_failure(
                request_id=request_id,
                message=exc.message,
                action="delete_table",
                remaining_tables=table_names,
            )

        if snapshot is None or safe_name not in snapshot.table_map():
            return self._action_failure(
                request_id=request_id,
                message=f"未找到数据表：{safe_name}，请从当前数据表中选择",
                action="delete_table",
                remaining_tables=table_names,
                candidates=table_names,
            )

        try:
            result = await anyio.to_thread.run_sync(
                partial(self._table_deletion.delete_table_tool, safe_name)
            )
        except (TableNotFoundError, TableDeletionError, TableImportUnavailableError) as exc:
            logger.info(
                "删表失败 request_id=%s table=%s error=%s",
                request_id,
                safe_name,
                exc.code,
            )
            return self._action_failure(
                request_id=request_id,
                message=exc.message,
                action="delete_table",
                remaining_tables=table_names,
            )

        logger.info("删表成功 request_id=%s table=%s", request_id, result.deleted_table)
        return ActionResultResponse(
            request_id=request_id,
            action="delete_table",
            success=True,
            message=f"已成功删除数据表：{result.deleted_table}",
            deleted_table=result.deleted_table,
            remaining_tables=list(result.remaining_tables),
        )

    async def _request_delete_all(
        self,
        request_id: str,
    ) -> ActionResultResponse | ConfirmationRequiredResponse:
        """删除全部表不立即执行，先签发绑定当前表集合的确认令牌。"""
        assert self._table_deletion is not None
        try:
            files, fingerprint = await anyio.to_thread.run_sync(
                self._table_deletion.table_set_fingerprint
            )
        except (TableDeletionError, TableImportUnavailableError) as exc:
            return self._action_failure(
                request_id=request_id,
                message=exc.message,
                action="delete_all_tables",
            )

        if not files:
            return self._action_failure(
                request_id=request_id,
                message="当前没有可删除的数据表",
                action="delete_all_tables",
            )

        confirmation = self._confirmation_store.issue(
            fingerprint=fingerprint,
            table_count=len(files),
        )
        logger.info(
            "删除全部表等待确认 request_id=%s table_count=%s",
            request_id,
            len(files),
        )
        return ConfirmationRequiredResponse(
            request_id=request_id,
            message="检测到您要删除所有数据表，此操作不可逆，是否确认？",
            confirmation_token=confirmation.token,
            expires_at=confirmation.expires_at,
            table_count=confirmation.table_count,
        )

    async def confirm_delete_all(
        self,
        *,
        confirmation_token: str,
        request_id: str,
    ) -> ActionResultResponse:
        """校验一次性令牌后清空工作区内的全部受管数据表。"""
        if self._table_deletion is None or self._table_catalog is None:
            return self._action_failure(
                request_id=request_id,
                message="多表数据目录未启用，无法删除数据表",
                action="delete_all_tables",
            )

        try:
            files, fingerprint = await anyio.to_thread.run_sync(
                self._table_deletion.table_set_fingerprint
            )
            outcome = self._confirmation_store.consume(
                confirmation_token,
                fingerprint=fingerprint,
            )
            if not outcome.ok:
                return self._action_failure(
                    request_id=request_id,
                    message=outcome.error or "确认信息无效，请重新发起删除全部表",
                    action="delete_all_tables",
                    remaining_tables=[path.removesuffix(".xlsx") for path in files],
                )

            result = await anyio.to_thread.run_sync(
                partial(
                    self._table_deletion.delete_all_tables,
                    expected_fingerprint=fingerprint,
                )
            )
        except (TableDeletionError, TableImportUnavailableError) as exc:
            logger.info(
                "删除全部表失败 request_id=%s error=%s",
                request_id,
                exc.code,
            )
            return self._action_failure(
                request_id=request_id,
                message=exc.message,
                action="delete_all_tables",
            )

        logger.info(
            "删除全部表成功 request_id=%s files=%s",
            request_id,
            len(result.deleted_files),
        )
        return ActionResultResponse(
            request_id=request_id,
            action="delete_all_tables",
            success=True,
            message=f"已成功删除全部数据表（{len(result.deleted_files)} 张）",
            deleted_tables=list(result.deleted_tables),
            remaining_tables=list(result.remaining_tables),
        )

    @staticmethod
    def _action_failure(
        *,
        request_id: str,
        message: str,
        action: str,
        remaining_tables: list[str] | None = None,
        candidates: list[str] | None = None,
    ) -> ActionResultResponse:
        return ActionResultResponse(
            request_id=request_id,
            action=action,  # type: ignore[arg-type]
            success=False,
            message=message,
            remaining_tables=list(remaining_tables or []),
            candidates=list(candidates or []),
        )

    async def handle(
        self,
        *,
        message: str,
        request_id: str,
        property_name: str | None = None,
        provider_type: str | None = None,
        user_api_key: str | SecretStr | None = None,
        model_name: str | None = None,
    ) -> CodeQueryResponse | ActionResultResponse | ConfirmationRequiredResponse:
        started_at = perf_counter()
        deadline = monotonic() + self._settings.code_total_timeout_seconds

        # BYOK：只有显式传了 provider/model/key 才解析覆盖配置；
        # 未传时保持原有“各组件自行使用系统默认”的行为。
        provider = self._resolve_provider(provider_type, user_api_key, model_name)
        llm_info = self._providers.describe(
            provider_type=provider_type,
            user_api_key=user_api_key,
            model_name=model_name,
        )

        # 先做语义理解：删除意图必须使用大模型识别，不再依赖正则匹配。
        snapshot, table_names = await self._table_context()
        try:
            understanding = await self._understanding.understand(
                message,
                table_names=table_names,
                provider=provider,
            )
        except TimeRangeParseError:
            # 无时间的普通汇总（如“汇总所有区域”）交给目录默认区间兜底；
            # 删除意图不会走到这里。
            understanding = QueryUnderstanding(
                intent="query",
                reason="未识别到时间范围",
            )

        action = await self._handle_intent(
            understanding,
            snapshot,
            table_names,
            request_id,
        )
        if action is not None:
            return action

        if not self._settings.code_query_enabled:
            raise CodeGenerationError(
                "生成式代码查询未启用",
                details={"code_query_enabled": False},
            )
        if not self._executor.available:
            raise CodeSandboxUnavailableError()

        extraction = understanding.time_extraction
        if extraction is None:
            # 多文件汇总（如“汇总所有区域的总工单量和总金额”）往往不带时间。
            # 目录模式有真实数据范围，可默认覆盖全部工单；RPA 单表模式
            # 仍要求用户明确时间，避免隐式放大查询窗口。
            extraction = await self._default_directory_extraction()
        _validate_range(extraction.time_range, self._settings)

        source = await self._resolve_source(
            time_range=extraction.time_range,
            property_name=property_name,
        )
        messages = build_initial_messages(message, source.profile)

        warnings = list(understanding.warnings) + list(extraction.warnings) + list(source.warnings)
        sample_warning = _time_diff_sample_warning(source.frames, message)
        if sample_warning:
            warnings.append(sample_warning)
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
                provider=provider,
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
                        timeout=min(self._settings.code_exec_timeout_seconds, remaining),
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

            chart_meta = infer_chart_meta(
                execution.columns,
                execution.rows,
                question=message,
            )
            download_url = await self._export_report(
                request_id=request_id,
                execution=execution,
                warnings=warnings,
            )
            summary = await self._build_report_summary(
                message=message,
                execution=execution,
                chart_meta=chart_meta,
                extraction=extraction,
                source=source,
                warnings=warnings,
                provider=provider,
            )
            word_download_url = await self._export_word_report(
                request_id=request_id,
                question=message,
                execution=execution,
                chart_meta=chart_meta,
                summary=summary,
                extraction=extraction,
                source=source,
                warnings=warnings,
            )

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
                chart_meta=chart_meta,
                download_url=download_url,
                word_download_url=word_download_url,
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
                    llm=LLMInfo(**llm_info),
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

    async def _export_report(
        self,
        *,
        request_id: str,
        execution: CodeExecutionResult,
        warnings: list[str],
    ) -> str | None:
        """导出 Excel；失败只降级为警告，不影响查询结果返回。"""
        if (
            self._report_exporter is None
            or not self._report_exporter.enabled
            or execution.row_count == 0
        ):
            return None

        try:
            exported = await anyio.to_thread.run_sync(
                partial(
                    self._report_exporter.export,
                    columns=execution.columns,
                    rows=execution.rows,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 导出失败不应中断查询
            logger.warning(
                "Excel 导出失败 request_id=%s error=%s",
                request_id,
                type(exc).__name__,
            )
            warnings.append("结果 Excel 导出失败，可继续查看页面结果")
            return None

        if exported is None:
            return None
        return f"{REPORT_DOWNLOAD_PATH}/{exported.filename}"

    def _resolve_provider(
        self,
        provider_type: str | None,
        user_api_key: str | SecretStr | None,
        model_name: str | None,
    ) -> ResolvedProvider | None:
        """仅在用户显式覆盖时解析 Provider，未知服务商在此返回 400。"""
        user_key = ""
        if isinstance(user_api_key, SecretStr):
            user_key = user_api_key.get_secret_value().strip()
        elif user_api_key:
            user_key = str(user_api_key).strip()

        has_override = bool((provider_type or "").strip() or (model_name or "").strip() or user_key)
        if not has_override:
            return None
        return self._providers.resolve(
            provider_type=provider_type,
            user_api_key=user_key or None,
            model_name=model_name,
        )

    async def _build_report_summary(
        self,
        *,
        message: str,
        execution: CodeExecutionResult,
        chart_meta: ChartMeta,
        extraction: TimeExtraction,
        source: _SourceData,
        warnings: list[str],
        provider: ResolvedProvider | None = None,
    ) -> ReportSummary | None:
        """生成报告结论；模型失败时退回规则摘要，不阻断 Word 生成。"""
        if not self._settings.word_report_enabled or execution.row_count == 0:
            return None

        fallback = ReportSummary(
            text=build_fallback_summary(
                columns=execution.columns,
                rows=execution.rows,
                chart_meta=chart_meta.model_dump(),
            ),
            engine="fallback",
        )
        if self._report_summarizer is None:
            return fallback

        try:
            summary = await self._report_summarizer.summarize(
                question=message,
                columns=execution.columns,
                rows=execution.rows,
                chart_meta=chart_meta.model_dump(),
                time_range=extraction.time_range.model_dump(),
                table_names=[table.name for table in source.tables],
                provider=provider,
            )
        except Exception:  # noqa: BLE001 - 总结失败不能影响主查询
            logger.exception("Word 报告总结异常")
            summary = fallback

        # 未配置模型时使用规则摘要属于正常降级，不产生“失败”告警。
        has_llm = provider is not None or bool(self._settings.deepseek_key)
        if summary.engine == "fallback" and has_llm:
            warnings.append("AI 总结生成失败，Word 报告已使用系统自动摘要")
        return summary

    async def _export_word_report(
        self,
        *,
        request_id: str,
        question: str,
        execution: CodeExecutionResult,
        chart_meta: ChartMeta,
        summary: ReportSummary | None,
        extraction: TimeExtraction,
        source: _SourceData,
        warnings: list[str],
    ) -> str | None:
        """导出 Word 报告；任何失败都只降级为警告。"""
        if (
            self._word_exporter is None
            or not self._word_exporter.enabled
            or execution.row_count == 0
            or summary is None
        ):
            return None

        table_names = [table.name for table in source.tables]
        if not table_names and source.primary_table:
            table_names = [source.primary_table]

        try:
            exported = await anyio.to_thread.run_sync(
                partial(
                    self._word_exporter.export,
                    question=question,
                    columns=execution.columns,
                    rows=execution.rows,
                    chart_meta=chart_meta.model_dump(),
                    summary=summary,
                    request_id=request_id,
                    time_range=extraction.time_range.model_dump(),
                    data_source=source.data_source,
                    table_names=tuple(table_names),
                    warnings=tuple(warnings),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 导出失败不应中断查询
            logger.warning(
                "Word 报告导出失败 request_id=%s error=%s",
                request_id,
                type(exc).__name__,
            )
            warnings.append("Word 报告生成失败，可先下载 Excel 结果")
            return None

        if exported is None:
            warnings.append("Word 报告生成失败，可先下载 Excel 结果")
            return None
        return f"{REPORT_DOWNLOAD_PATH}/{exported.filename}"

    async def _default_directory_extraction(self) -> TimeExtraction:
        """时间解析失败时，用数据目录覆盖的完整区间兜底。

        只对多表目录生效：目录里已有用户上传的 Excel，默认汇总全部数据
        比直接返回 422 更符合经营分析的意图；RPA 单表链路保持显式时间。
        """
        if self._table_catalog is None:
            raise TimeRangeParseError("未识别到时间范围，请补充如「上个月」「近 7 天」")
        snapshot = await anyio.to_thread.run_sync(self._table_catalog.scan)
        bounds = _snapshot_time_bounds(snapshot)
        if bounds is None:
            raise TimeRangeParseError("未识别到时间范围，且数据目录中没有可用的时间列")

        start_date, end_date = bounds
        return TimeExtraction(
            time_range=TimeRange(start_date=start_date, end_date=end_date),
            engine="rule_based",
            model=None,
            confidence=1.0,
            reason="未指定时间，默认使用数据目录覆盖的完整区间",
            warnings=(f"未识别到时间范围，已默认汇总 {start_date} 至 {end_date} 的全部数据",),
        )

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

        # 同构表组内的每张表都是事实表，必须分别按时间/项目过滤；
        # 只过滤主表会让其他文件保留全量数据，导致跨文件汇总失真。
        primary_signature = tuple(str(column) for column in primary_dataset.frame.columns)
        fact_names = {
            table.name
            for table in snapshot.tables
            if table.role == "fact"
            or tuple(str(column) for column in table.frame.columns) == primary_signature
        }

        filter_warnings: list[str] = []
        time_filtered = False
        for table in snapshot.tables:
            if table.name not in fact_names:
                continue
            filtered, table_warnings, table_time_filtered = _filter_fact_frame(
                frames[table.name],
                table.time_column,
                time_range=time_range,
                property_name=property_name,
                label=f"表 {table.name}",
            )
            frames[table.name] = filtered
            filter_warnings.extend(table_warnings)
            time_filtered = time_filtered or table_time_filtered

        total_filtered = sum(len(frames[name]) for name in fact_names)

        profile = build_catalog_profile(
            snapshot.tables,
            frames=frames,
            time_range=time_range,
            data_source="directory",
            primary_table=primary,
            time_filtered=time_filtered,
            relationships=snapshot.relationships,
            homogeneous_groups=snapshot.homogeneous_groups,
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
        if total_filtered == 0:
            warnings.append("同构工单表在所选时间/项目条件下没有数据")

        return _SourceData(
            frames=frames,
            primary_table=primary,
            profile=profile,
            data_source="directory",
            fetched_at=datetime.now(self._settings.tz),
            source_row_count=int(total_filtered),
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


def _filter_fact_frame(
    frame: pd.DataFrame,
    time_column: str | None,
    *,
    time_range: TimeRange,
    property_name: str | None,
    label: str,
) -> tuple[pd.DataFrame, list[str], bool]:
    """按时间/项目过滤一张事实表，维表不调用此函数。"""
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
                warnings.append(f"{label} 有 {dropped} 行登记时间超出查询范围，已排除")
        else:
            warnings.append(f"{label} 的时间列 {time_column} 无法解析，未按时间过滤")
    elif time_column:
        warnings.append(f"{label} 的时间列 {time_column} 不存在，未按时间过滤")
    else:
        warnings.append(f"{label} 未识别到时间列，未按时间过滤")

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
            warnings.append(f"{label} 没有项目名称列，已忽略项目过滤条件")

    return filtered.reset_index(drop=True), warnings, time_filtered


def _snapshot_time_bounds(
    snapshot: TableSnapshot,
) -> tuple[date, date] | None:
    """汇总事实表时间列的全局最小/最大值。

    同构表可能来自不同月份，取所有文件并集才能覆盖完整数据。
    """
    fact_names = {table.name for table in snapshot.tables if table.role == "fact"}
    for group in snapshot.homogeneous_groups:
        fact_names.update(str(name) for name in group.get("tables", ()))

    lower: pd.Timestamp | None = None
    upper: pd.Timestamp | None = None
    for table in snapshot.tables:
        if fact_names and table.name not in fact_names:
            continue
        if not table.time_column or table.time_column not in table.frame.columns:
            continue
        parsed = pd.to_datetime(table.frame[table.time_column], errors="coerce")
        parsed = parsed.dropna()
        if parsed.empty:
            continue
        table_min = parsed.min()
        table_max = parsed.max()
        lower = table_min if lower is None else min(lower, table_min)
        upper = table_max if upper is None else max(upper, table_max)

    if lower is None or upper is None:
        return None
    return lower.date(), upper.date()


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


_TIME_DIFF_KEYWORDS = (
    "时长",
    "耗时",
    "时间差",
    "间隔",
    "完成时间",
    "处理时间",
    "多久",
    "小时",
    "分钟",
)


def _time_diff_sample_warning(frames: dict[str, pd.DataFrame], question: str) -> str | None:
    """时间差类问题若完成时间缺失严重，提示指标样本覆盖不足。

    只在用户询问时长/间隔等时间差指标时触发；完成时间缺失超过 20%
    认为样本存在明显偏差，避免用户把局部均值当成全量结论。
    """
    if not any(keyword in question for keyword in _TIME_DIFF_KEYWORDS):
        return None

    seen: set[int] = set()
    total = 0
    completed = 0
    for frame in frames.values():
        if id(frame) in seen or "完成时间" not in frame.columns:
            continue
        seen.add(id(frame))
        total += int(len(frame))
        completed += int(pd.to_datetime(frame["完成时间"], errors="coerce").notna().sum())

    if total == 0 or completed >= total:
        return None
    if (total - completed) / total < 0.2:
        return None
    return f"当前时间差指标仅基于已完成的 {completed} 条工单计算，存在样本缺失"


def _dedupe(values: list[str]) -> list[str]:
    """保持警告顺序并去重，避免重试过程重复刷屏。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
