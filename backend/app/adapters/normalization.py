"""将不同数据源的结果收敛为统一工单表。"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from app.core.exceptions import DataContractError
from app.domain.work_orders import REQUIRED_WORK_ORDER_COLUMNS, WorkOrderColumn
from app.schemas.assistant import TimeRange

logger = logging.getLogger(__name__)


def normalize_work_order_frame(
    frame: pd.DataFrame,
    time_range: TimeRange,
    property_name: str | None = None,
) -> pd.DataFrame:
    """校验列契约、规范化类型，并执行最终的时间/项目过滤。

    适配器各自的过滤逻辑可能因导出规则不同而有偏差，这里是统一兜底，
    确保进入 Pandas 汇总的数据严格落在用户请求范围内。
    """
    missing = [column for column in REQUIRED_WORK_ORDER_COLUMNS if column not in frame.columns]
    if missing:
        raise DataContractError(
            details={"missing_columns": missing},
        )

    normalized = frame.copy()
    received_column = WorkOrderColumn.RECEIVED_AT.value

    # 脏日期不参与统计，但保留其余合法数据。
    received_at = pd.to_datetime(normalized[received_column], errors="coerce")
    invalid_date_count = int(received_at.isna().sum())
    if invalid_date_count:
        logger.warning("丢弃 %s 条受理时间无效的数据", invalid_date_count)

    normalized = normalized.loc[received_at.notna()].copy()
    normalized[received_column] = received_at.loc[received_at.notna()]

    start_at = pd.Timestamp(time_range.start_date)
    # end_date 按闭区间处理：结束日次日 00:00 之前均属于范围内。
    end_exclusive = pd.Timestamp(time_range.end_date + timedelta(days=1))
    normalized = normalized.loc[
        (normalized[received_column] >= start_at) & (normalized[received_column] < end_exclusive)
    ]

    project_column = WorkOrderColumn.PROJECT.value
    if property_name:
        project_mask = (
            normalized[project_column]
            .astype("string")
            .str.strip()
            .eq(property_name.strip())
            .fillna(False)
        )
        normalized = normalized.loc[project_mask]

    amount_column = WorkOrderColumn.AMOUNT.value
    normalized[amount_column] = pd.to_numeric(normalized[amount_column], errors="coerce").fillna(
        0.0
    )

    satisfaction_column = WorkOrderColumn.SATISFACTION.value
    if satisfaction_column in normalized.columns:
        normalized[satisfaction_column] = pd.to_numeric(
            normalized[satisfaction_column], errors="coerce"
        )

    completed_column = WorkOrderColumn.COMPLETED_AT.value
    if completed_column in normalized.columns:
        normalized[completed_column] = pd.to_datetime(normalized[completed_column], errors="coerce")

    return normalized.sort_values(WorkOrderColumn.RECEIVED_AT.value).reset_index(drop=True)
