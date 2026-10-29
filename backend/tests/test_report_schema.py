from datetime import date

import pandas as pd

from app.adapters.report_schema import normalize_report_frame
from app.domain.work_orders import WorkOrderColumn
from app.schemas.assistant import TimeRange


def test_report_frame_maps_raw_columns_to_standard_contract() -> None:
    frame = pd.DataFrame(
        [
            {
                "单号": "20260901-001",
                "登记时间": "2026-09-01 09:00:00.0",
                "项目名称": "望京国际研发园",
                "工单类型": "数智中枢告警工单",
                "处理状态": "已处理",
                "维修人员": "",
                "接单人": "张伟",
                "登记人": "Admin",
                "满意度": "非常满意",
                "完成时间": "2026-09-01 09:30:00.0",
            },
            {
                "单号": "YFY-20260901-G-001",
                "登记时间": "2026-09-01 10:00:00.0",
                "项目名称": "望京国际研发园",
                "工单类型": "提交工单",
                "处理状态": "处理中",
                "工单服务项目": "【服务项目】提交工单/工程维修",
                "服务内容": "【业务类型】提交工单/工程维修/强电专业维修/灯具维修[]",
                "维修人员": "",
                "接单人": "李强",
                "登记人": "王芳",
                "满意度": "",
                "完成时间": "",
            },
            {
                "单号": "YFY-20260901-B-001",
                "登记时间": "2026-09-01 11:00:00.0",
                "项目名称": "望京国际研发园",
                "工单类型": "住宅报单",
                "处理状态": "已作废",
                "维修人员": "",
                "接单人": "",
                "登记人": "赵敏",
                "满意度": "",
                "完成时间": "",
            },
        ]
    )

    result = normalize_report_frame(
        frame,
        TimeRange(start_date=date(2026, 9, 1), end_date=date(2026, 9, 1)),
        "望京国际研发园",
    )

    assert result[WorkOrderColumn.REPAIR_TYPE.value].tolist() == [
        "安防告警",
        "工程维修",
        "住宅报修",
    ]
    assert result[WorkOrderColumn.STATUS.value].tolist() == ["已完成", "处理中", "已取消"]
    assert result.loc[0, WorkOrderColumn.AMOUNT.value] == 0.0
    assert result.loc[1, WorkOrderColumn.AMOUNT.value] > 0.0
    assert result.loc[2, WorkOrderColumn.AMOUNT.value] == 0.0
    assert result.loc[0, WorkOrderColumn.SATISFACTION.value] == 5.0
    assert result.loc[1, WorkOrderColumn.OWNER.value] == "李强"
    assert result.loc[2, WorkOrderColumn.OWNER.value] == "赵敏"
