"""RPA 浏览器驱动的可替换实现。

第一阶段使用 ``SimulatedRpaExportDriver`` 生成结构真实的 Excel，
以验证「下载文件 -> Pandas -> 汇总 -> 网关」全链路。

接入真实系统时，新建 ``PlaywrightRpaExportDriver`` 并实现同一 Protocol，
在构造 RPAFetcher 时注入即可；RPAFetcher 无需修改。
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

from app.adapters.base import FetchRequest
from app.domain.work_orders import WorkOrderColumn

logger = logging.getLogger(__name__)


class RpaExportDriver(Protocol):
    """RPA 下载动作抽象。"""

    def download_work_orders(self, request: FetchRequest, download_dir: Path) -> Path:
        """执行页面登录、筛选、导出，并返回下载到本地的 Excel 路径。"""
        ...


class SimulatedRpaExportDriver:
    """仅用于第一阶段的 RPA 模拟器。

    生成的数据具备真实业务字段，且全部落在请求时间范围内，
    方便后续直接用真实浏览器自动化替换。
    """

    _PROJECTS = ("樱花苑", "翠竹园", "星河湾", "梧桐里")
    _REPAIR_TYPES = ("水电维修", "公共设施", "环境卫生", "安防门禁", "电梯维保")
    _OWNERS = ("王工", "李工", "赵工", "陈工", "周工")
    _STATUSES = ("已完成", "处理中", "已取消")

    def __init__(self, simulate_delay_seconds: float = 0.0) -> None:
        self._simulate_delay_seconds = simulate_delay_seconds

    def download_work_orders(self, request: FetchRequest, download_dir: Path) -> Path:
        if self._simulate_delay_seconds:
            # 模拟浏览器打开、登录、点击筛选和下载的耗时。
            time.sleep(self._simulate_delay_seconds)

        download_dir.mkdir(parents=True, exist_ok=True)
        time_range = request.time_range
        file_name = (
            f"property_work_orders_{time_range.start_date.isoformat()}_"
            f"{time_range.end_date.isoformat()}_{uuid.uuid4().hex[:8]}.xlsx"
        )
        target = download_dir / file_name

        frame = self._build_frame(
            start_date=time_range.start_date,
            end_date=time_range.end_date,
            property_name=request.property_name,
        )
        with pd.ExcelWriter(target, engine="openpyxl") as writer:
            frame.to_excel(writer, sheet_name="工单明细", index=False)

        logger.info("RPA 模拟下载完成: %s, rows=%s", target.name, len(frame))
        return target

    def _build_frame(
        self,
        *,
        start_date: date,
        end_date: date,
        property_name: str | None,
    ) -> pd.DataFrame:
        """生成可复现的样例数据，同一天查询结果保持稳定。"""
        seed = f"{start_date.isoformat()}:{end_date.isoformat()}:{property_name or '*'}"
        rng = random.Random(seed)
        records: list[dict[str, object]] = []
        current = start_date

        while current <= end_date:
            daily_count = rng.choices((2, 3, 4, 5), weights=(20, 40, 30, 10), k=1)[0]
            for sequence in range(1, daily_count + 1):
                status = rng.choices(self._STATUSES, weights=(78, 15, 7), k=1)[0]
                received_at = datetime.combine(
                    current,
                    dt_time(
                        hour=rng.randint(8, 19),
                        minute=rng.randint(0, 59),
                        second=rng.randint(0, 59),
                    ),
                )

                completed_at: datetime | None = None
                satisfaction: float | None = None
                if status == "已完成":
                    completed_at = received_at + timedelta(minutes=rng.randint(20, 60 * 72))
                    satisfaction = rng.choice((3.0, 3.5, 4.0, 4.5, 5.0))

                amount = 0.0 if status == "已取消" else round(rng.uniform(50, 1600), 2)
                records.append(
                    {
                        WorkOrderColumn.TICKET_ID.value: (
                            f"WO{current.strftime('%Y%m%d')}{sequence:03d}"
                        ),
                        WorkOrderColumn.PROJECT.value: property_name or rng.choice(self._PROJECTS),
                        WorkOrderColumn.REPAIR_TYPE.value: rng.choice(self._REPAIR_TYPES),
                        WorkOrderColumn.STATUS.value: status,
                        WorkOrderColumn.RECEIVED_AT.value: received_at,
                        WorkOrderColumn.COMPLETED_AT.value: completed_at,
                        WorkOrderColumn.AMOUNT.value: amount,
                        WorkOrderColumn.SATISFACTION.value: satisfaction,
                        WorkOrderColumn.OWNER.value: rng.choice(self._OWNERS),
                    }
                )
            current += timedelta(days=1)

        return pd.DataFrame.from_records(records)
