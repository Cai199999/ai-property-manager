"""基于浏览器自动化的 Excel 数据抓取器。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pandas as pd

from app.adapters.base import DataFetcher, FetchRequest, FetchResult
from app.adapters.normalization import normalize_work_order_frame
from app.adapters.rpa_driver import RpaExportDriver, SimulatedRpaExportDriver
from app.core.exceptions import DataFetchError

logger = logging.getLogger(__name__)


def read_work_order_excel(path: Path) -> pd.DataFrame:
    """读取工单 Excel，并兼容「标题行 + 表头行」与普通单行表头。

    真实「工单汇总查询」导出件第 1 行是合并标题，第 2 行才是列名；
    模拟器和真实系统共用此读取逻辑，避免把标题误当成 DataFrame 列名。
    """
    raw = pd.read_excel(path, engine="openpyxl", header=None, dtype=object)
    if raw.empty:
        return raw

    header_index = 0
    for index in range(min(10, len(raw))):
        values = {
            str(value).strip()
            for value in raw.iloc[index].tolist()
            if pd.notna(value) and str(value).strip()
        }
        if {"单号", "登记时间"}.issubset(values):
            header_index = index
            break

    headers: list[str] = []
    for index, value in enumerate(raw.iloc[header_index].tolist(), start=1):
        name = str(value).strip() if pd.notna(value) else ""
        headers.append(name or f"未命名列{index}")

    frame = raw.iloc[header_index + 1 :].copy()
    frame.columns = headers
    return frame.dropna(how="all").reset_index(drop=True)


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
                read_work_order_excel,
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
