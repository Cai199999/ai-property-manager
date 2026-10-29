"""工单明细的 Pandas 汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.exceptions import DataContractError
from app.domain.work_orders import (
    COMPLETED_STATUS,
    PREVIEW_WORK_ORDER_COLUMNS,
    REQUIRED_WORK_ORDER_COLUMNS,
    WorkOrderColumn,
)
from app.schemas.assistant import GroupSummary, WorkOrderSummary


@dataclass(slots=True)
class SummaryResult:
    summary: WorkOrderSummary
    records_preview: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


def summarize_work_orders(
    frame: pd.DataFrame, *, preview_rows: int = 20
) -> SummaryResult:
    """生成核心经营指标、多维分组，以及供页面展示的明细预览。"""
    missing = [
        column for column in REQUIRED_WORK_ORDER_COLUMNS if column not in frame.columns
    ]
    if missing:
        raise DataContractError(details={"missing_columns": missing})

    total_records = int(len(frame))
    warnings: list[str] = []
    if total_records == 0:
        warnings.append("查询时间范围内没有符合条件的工单数据")

    amount_column = WorkOrderColumn.AMOUNT.value
    status_column = WorkOrderColumn.STATUS.value
    satisfaction_column = WorkOrderColumn.SATISFACTION.value
    total_amount = round(float(frame[amount_column].sum()), 2) if total_records else 0.0

    completed_mask = frame[status_column].astype("string").eq(COMPLETED_STATUS)
    completed_count = int(completed_mask.sum())
    completion_rate = (
        round(completed_count / total_records, 4) if total_records else 0.0
    )

    satisfaction: float | None = None
    if satisfaction_column in frame.columns:
        valid_satisfaction = pd.to_numeric(
            frame[satisfaction_column], errors="coerce"
        ).dropna()
        if not valid_satisfaction.empty:
            satisfaction = round(float(valid_satisfaction.mean()), 2)

    return SummaryResult(
        summary=WorkOrderSummary(
            total_records=total_records,
            total_amount=total_amount,
            completed_count=completed_count,
            completion_rate=completion_rate,
            average_satisfaction=satisfaction,
            by_project=_group_summary(frame, WorkOrderColumn.PROJECT.value),
            by_repair_type=_group_summary(frame, WorkOrderColumn.REPAIR_TYPE.value),
            by_status=_group_summary(frame, status_column),
        ),
        records_preview=_preview(frame, preview_rows),
        warnings=warnings,
    )


def _group_summary(frame: pd.DataFrame, column: str) -> list[GroupSummary]:
    if frame.empty:
        return []

    working = pd.DataFrame(
        {
            "name": frame[column].astype("string").fillna("未填写").str.strip(),
            "amount": pd.to_numeric(
                frame[WorkOrderColumn.AMOUNT.value], errors="coerce"
            ).fillna(0.0),
        }
    )
    grouped = (
        working.groupby("name", dropna=False)
        .agg(count=("amount", "size"), amount=("amount", "sum"))
        .reset_index()
        .sort_values(["count", "amount"], ascending=[False, False])
    )
    return [
        GroupSummary(
            name=str(row["name"]),
            count=int(row["count"]),
            amount=round(float(row["amount"]), 2),
        )
        for _, row in grouped.iterrows()
    ]


def _preview(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or frame.empty:
        return []

    columns = [
        column for column in PREVIEW_WORK_ORDER_COLUMNS if column in frame.columns
    ]
    records = frame.loc[:, columns].head(limit).to_dict(orient="records")
    return [{key: _json_safe(value) for key, value in row.items()} for row in records]


def _json_safe(value: Any) -> Any:
    """将 Pandas/NumPy 标量转为严格 JSON 可序列化的值。"""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value
