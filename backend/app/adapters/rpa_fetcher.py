"""基于浏览器自动化的 Excel 数据抓取器。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import anyio
import pandas as pd

from app.adapters.base import DataFetcher, FetchRequest, FetchResult
from app.adapters.normalization import normalize_work_order_frame
from app.adapters.rpa_driver import RpaExportDriver, SimulatedRpaExportDriver
from app.core.exceptions import DataFetchError

logger = logging.getLogger(__name__)


class RPAFetcher(DataFetcher):
    """RPA 数据源适配器。

    RPAFetcher 只负责「驱动下载 + 读取 Excel + 规范化」；
    具体如何登录、筛选和点击导出由注入的 ``RpaExportDriver`` 决定。
    """

    source = "rpa"

    def __init__(
        self,
        download_dir: Path,
        *,
        simulate_delay_seconds: float = 0.0,
        driver: RpaExportDriver | None = None,
    ) -> None:
        self._download_dir = download_dir
        self._driver = driver or SimulatedRpaExportDriver(simulate_delay_seconds)

    async def fetch(self, request: FetchRequest) -> FetchResult:
        started_at = datetime.now(timezone.utc)

        try:
            downloaded_file = await anyio.to_thread.run_sync(
                self._driver.download_work_orders,
                request,
                self._download_dir,
            )
            frame = await anyio.to_thread.run_sync(
                partial(pd.read_excel, engine="openpyxl"),
                downloaded_file,
            )
        except DataFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - 适配器边界统一包装为业务异常
            logger.exception("RPA 下载或读取 Excel 失败")
            raise DataFetchError("RPA 下载或读取 Excel 失败") from exc

        normalized = await anyio.to_thread.run_sync(
            normalize_work_order_frame,
            frame,
            request.time_range,
            request.property_name,
        )

        logger.info(
            "RPA 数据就绪, file=%s, rows=%s, elapsed=%.2fs",
            downloaded_file.name,
            len(normalized),
            (datetime.now(timezone.utc) - started_at).total_seconds(),
        )
        return FetchResult(
            frame=normalized,
            source=self.source,
            fetched_at=datetime.now(timezone.utc),
            metadata={
                "source_file": downloaded_file.name,
                "download_mode": "simulated",
                "raw_row_count": len(frame),
            },
        )

    async def healthcheck(self) -> bool:
        return await asyncio.to_thread(self._download_dir.parent.exists)
