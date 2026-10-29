"""工单数据的统一领域契约。

所有 DataFetcher（RPA、API、未来的消息队列等）都必须输出下列列名，
这样上层 Agent、Pandas 汇总和 Java 网关无需感知底层数据来源。
"""

from __future__ import annotations

from enum import StrEnum


class WorkOrderColumn(StrEnum):
    """工单明细的标准列名。"""

    TICKET_ID = "工单编号"
    PROJECT = "项目名称"
    REPAIR_TYPE = "报修类型"
    STATUS = "状态"
    RECEIVED_AT = "受理时间"
    COMPLETED_AT = "完成时间"
    AMOUNT = "费用金额"
    SATISFACTION = "满意度"
    OWNER = "负责人"


# 汇总计算必须依赖的最小列集合；DataFetcher 在返回 DataFrame 前应保证存在。
REQUIRED_WORK_ORDER_COLUMNS: tuple[str, ...] = (
    WorkOrderColumn.TICKET_ID.value,
    WorkOrderColumn.PROJECT.value,
    WorkOrderColumn.REPAIR_TYPE.value,
    WorkOrderColumn.STATUS.value,
    WorkOrderColumn.RECEIVED_AT.value,
    WorkOrderColumn.AMOUNT.value,
)

# 允许出现在 API 预览和 Excel 导出中的列。
PREVIEW_WORK_ORDER_COLUMNS: tuple[str, ...] = tuple(
    column.value for column in WorkOrderColumn
)

COMPLETED_STATUS = "已完成"
