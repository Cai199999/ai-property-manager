from datetime import datetime

import pandas as pd
import pytest

from app.domain.work_orders import WorkOrderColumn
from app.services.summary import summarize_work_orders


def _sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                WorkOrderColumn.TICKET_ID.value: "WO001",
                WorkOrderColumn.PROJECT.value: "樱花苑",
                WorkOrderColumn.REPAIR_TYPE.value: "水电维修",
                WorkOrderColumn.STATUS.value: "已完成",
                WorkOrderColumn.RECEIVED_AT.value: datetime(2026, 9, 1, 9, 0),
                WorkOrderColumn.COMPLETED_AT.value: datetime(2026, 9, 1, 11, 0),
                WorkOrderColumn.AMOUNT.value: 100.0,
                WorkOrderColumn.SATISFACTION.value: 5.0,
                WorkOrderColumn.OWNER.value: "王工",
            },
            {
                WorkOrderColumn.TICKET_ID.value: "WO002",
                WorkOrderColumn.PROJECT.value: "樱花苑",
                WorkOrderColumn.REPAIR_TYPE.value: "公共设施",
                WorkOrderColumn.STATUS.value: "处理中",
                WorkOrderColumn.RECEIVED_AT.value: datetime(2026, 9, 2, 10, 0),
                WorkOrderColumn.COMPLETED_AT.value: None,
                WorkOrderColumn.AMOUNT.value: 50.5,
                WorkOrderColumn.SATISFACTION.value: None,
                WorkOrderColumn.OWNER.value: "李工",
            },
            {
                WorkOrderColumn.TICKET_ID.value: "WO003",
                WorkOrderColumn.PROJECT.value: "翠竹园",
                WorkOrderColumn.REPAIR_TYPE.value: "水电维修",
                WorkOrderColumn.STATUS.value: "已完成",
                WorkOrderColumn.RECEIVED_AT.value: datetime(2026, 9, 3, 12, 0),
                WorkOrderColumn.COMPLETED_AT.value: datetime(2026, 9, 3, 13, 0),
                WorkOrderColumn.AMOUNT.value: 200.0,
                WorkOrderColumn.SATISFACTION.value: 4.0,
                WorkOrderColumn.OWNER.value: "赵工",
            },
        ]
    )


def test_summarize_work_orders_calculates_kpis() -> None:
    result = summarize_work_orders(_sample_frame(), preview_rows=2)

    assert result.summary.total_records == 3
    assert result.summary.total_amount == 350.5
    assert result.summary.completed_count == 2
    assert result.summary.completion_rate == pytest.approx(0.6667)
    assert result.summary.average_satisfaction == 4.5
    assert len(result.records_preview) == 2
    assert result.records_preview[0]["工单编号"] == "WO001"


def test_summary_groups_are_sorted_by_count() -> None:
    result = summarize_work_orders(_sample_frame())

    assert result.summary.by_project[0].name == "樱花苑"
    assert result.summary.by_project[0].count == 2
    assert result.summary.by_repair_type[0].name == "水电维修"
    assert {item.name for item in result.summary.by_status} == {
        "已完成",
        "处理中",
    }


def test_empty_data_returns_zero_summary() -> None:
    frame = _sample_frame().iloc[0:0].copy()

    result = summarize_work_orders(frame)

    assert result.summary.total_records == 0
    assert result.summary.completion_rate == 0.0
    assert result.summary.average_satisfaction is None
    assert result.warnings
