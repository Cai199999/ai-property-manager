"""助手编排层：解析时间 -> 获取数据 -> Pandas 汇总。"""

from __future__ import annotations

import logging
from functools import partial
from time import perf_counter

import anyio

from app.adapters.base import DataFetcher, FetchRequest
from app.config import Settings
from app.core.exceptions import TimeRangeParseError
from app.schemas.assistant import (
    AssistantQueryResponse,
    ParserInfo,
    QueryMeta,
    TimeRange,
)
from app.services.deepseek_extractor import DeepSeekTimeRangeExtractor
from app.services.summary import summarize_work_orders

logger = logging.getLogger(__name__)


class AssistantAgent:
    """无状态编排器，可在多个请求间安全复用。"""

    def __init__(
        self,
        settings: Settings,
        extractor: DeepSeekTimeRangeExtractor,
        fetcher: DataFetcher,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._fetcher = fetcher

    async def handle(
        self,
        *,
        message: str,
        request_id: str,
        property_name: str | None = None,
    ) -> AssistantQueryResponse:
        started_at = perf_counter()

        extraction = await self._extractor.extract(message)
        self._validate_range(extraction.time_range)

        fetch_result = await self._fetcher.fetch(
            FetchRequest(
                time_range=extraction.time_range,
                property_name=property_name,
            )
        )
        summary_result = await anyio.to_thread.run_sync(
            partial(
                summarize_work_orders,
                preview_rows=self._settings.preview_rows,
            ),
            fetch_result.frame,
        )

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        warnings = [*extraction.warnings, *summary_result.warnings]
        logger.info(
            "查询完成 request_id=%s source=%s engine=%s rows=%s elapsed_ms=%s",
            request_id,
            fetch_result.source,
            extraction.engine,
            len(fetch_result.frame),
            elapsed_ms,
        )

        return AssistantQueryResponse(
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
            summary=summary_result.summary,
            records_preview=summary_result.records_preview,
            meta=QueryMeta(
                fetched_at=fetch_result.fetched_at.astimezone(self._settings.tz),
                elapsed_ms=elapsed_ms,
                row_count=len(fetch_result.frame),
                source_file=fetch_result.metadata.get("source_file"),
                warnings=warnings,
            ),
        )

    def _validate_range(self, time_range: TimeRange) -> None:
        """防止超大范围拖垮 RPA/Pandas，限制在配置的最大天数内。"""
        days = (time_range.end_date - time_range.start_date).days + 1
        if days > self._settings.max_query_days:
            raise TimeRangeParseError(
                f"查询范围过大：{days} 天，最多支持 {self._settings.max_query_days} 天",
                details={
                    "requested_days": days,
                    "max_query_days": self._settings.max_query_days,
                },
            )
