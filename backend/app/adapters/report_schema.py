"""真实「工单汇总查询」报表的结构契约与规范化。

真实 RPA 导出件的版式：
- 第 1 行：合并单元格标题「工单汇总查询」；
- 第 2 行：46 个业务列名；
- 第 3 行起：工单明细。

模拟器按该版式生成 Excel，本模块负责把原始中文列收敛为平台统一的
``WorkOrderColumn``，上层 Agent 和 Pandas 汇总无需感知具体报表格式。
"""

from __future__ import annotations

import hashlib

import pandas as pd

from app.core.exceptions import DataContractError
from app.domain.work_orders import WorkOrderColumn
from app.schemas.assistant import TimeRange

REPORT_SHEET_NAME = "Sheet0"
REPORT_TITLE = "工单汇总查询"

# 列顺序必须与真实导出文件保持一致，模拟器按此顺序写表。
REPORT_COLUMNS: tuple[str, ...] = (
    "单号",
    "登记时间",
    "项目名称",
    "工单类型",
    "处理状态",
    "流程名称",
    "登记人",
    "客户名称",
    "房间号",
    "联系人",
    "联系电话",
    "详细地址",
    "户内公区",
    "工单服务项目",
    "服务内容",
    "问题描述",
    "紧急程度",
    "数据来源",
    "预约时间",
    "收单时间",
    "接单时间",
    "接单人",
    "维修人员",
    "到场时间",
    "完成时间",
    "处理内容",
    "是否回访",
    "回访方式",
    "回访人员",
    "回访时间",
    "满意度",
    "当前节点",
    "未处理",
    "当前节点人",
    "任务时间",
    "使用材料",
    "物业部时效",
    "工程部时效",
    "安保部时效",
    "接单时效",
    "到达现场时效",
    "处理时效",
    "物业经理派单时间",
    "工程经理派单时间",
    "安保经理派单时间",
    "未完成原因",
)

# 真实文件的列宽（字符宽度），模拟器写表时直接复用。
REPORT_COLUMN_WIDTHS: dict[str, float] = {
    "单号": 23.4375,
    "登记时间": 23.4375,
    "项目名称": 23.4375,
    "工单类型": 15.625,
    "处理状态": 18.75,
    "流程名称": 18.75,
    "登记人": 15.625,
    "客户名称": 18.75,
    "房间号": 18.75,
    "联系人": 18.75,
    "联系电话": 18.75,
    "详细地址": 18.75,
    "户内公区": 18.75,
    "工单服务项目": 18.75,
    "服务内容": 39.0625,
    "问题描述": 39.0625,
    "紧急程度": 18.75,
    "数据来源": 18.75,
    "预约时间": 18.75,
    "收单时间": 23.4375,
    "接单时间": 23.4375,
    "接单人": 18.75,
    "维修人员": 18.75,
    "到场时间": 23.4375,
    "完成时间": 23.4375,
    "处理内容": 18.75,
    "是否回访": 18.75,
    "回访方式": 18.75,
    "回访人员": 18.75,
    "回访时间": 23.4375,
    "满意度": 18.75,
    "当前节点": 18.75,
    "未处理": 18.75,
    "当前节点人": 18.75,
    "任务时间": 23.4375,
    "使用材料": 18.75,
    "物业部时效": 18.75,
    "工程部时效": 18.75,
    "安保部时效": 18.75,
    "接单时效": 18.75,
    "到达现场时效": 18.75,
    "处理时效": 18.75,
    "物业经理派单时间": 23.4375,
    "工程经理派单时间": 23.4375,
    "安保经理派单时间": 23.4375,
    "未完成原因": 18.75,
}

# 用于识别「真实报表结构」，避免与旧的 9 列标准表混淆。
REPORT_MARKER_COLUMNS = frozenset({"单号", "登记时间", "项目名称", "处理状态"})
REQUIRED_REPORT_COLUMNS: tuple[str, ...] = (
    "单号",
    "登记时间",
    "项目名称",
    "处理状态",
)

# 原始系统状态 -> 平台统一状态。
STATUS_MAPPING: dict[str, str] = {
    "已处理": "已完成",
    "处理中": "处理中",
    "草稿": "处理中",
    "已作废": "已取消",
}

# 原始系统只提供满意度档位，平台统一为 1~5 分。
SATISFACTION_MAPPING: dict[str, float] = {
    "非常满意": 5.0,
    "满意": 4.0,
    "一般": 3.0,
    "不满意": 2.0,
    "非常不满意": 1.0,
}


def looks_like_report_frame(frame: pd.DataFrame) -> bool:
    """判断 DataFrame 是否来自真实「工单汇总查询」报表。"""
    columns = {str(column).strip() for column in frame.columns}
    return REPORT_MARKER_COLUMNS.issubset(columns)


def normalize_report_frame(
    frame: pd.DataFrame,
    time_range: TimeRange,
    property_name: str | None = None,
) -> pd.DataFrame:
    """把 46 列原始报表转换为统一工单表。

    真实报表没有「费用金额」和数值型「满意度」列，因此这里按工单类型
    生成可复现的模拟费用，并把满意度档位映射为 1~5 分。这样既保留原始
    导出结构，又能让上层看板继续计算经营指标。
    """
    missing = [column for column in REQUIRED_REPORT_COLUMNS if column not in frame.columns]
    if missing:
        raise DataContractError(details={"missing_columns": missing})

    source = frame.copy()
    received_at = pd.to_datetime(source["登记时间"], errors="coerce")
    valid_time_mask = received_at.notna()

    result = pd.DataFrame(index=source.index)
    result[WorkOrderColumn.TICKET_ID.value] = _clean_text(source, "单号")
    result[WorkOrderColumn.PROJECT.value] = _clean_text(source, "项目名称")
    result[WorkOrderColumn.REPAIR_TYPE.value] = _derive_repair_type(source)
    result[WorkOrderColumn.STATUS.value] = _derive_status(source)
    result[WorkOrderColumn.RECEIVED_AT.value] = received_at
    result[WorkOrderColumn.COMPLETED_AT.value] = pd.to_datetime(
        _clean_text(source, "完成时间"), errors="coerce"
    )
    result[WorkOrderColumn.OWNER.value] = _derive_owner(source)

    satisfaction = _clean_text(source, "满意度").map(SATISFACTION_MAPPING)
    result[WorkOrderColumn.SATISFACTION.value] = pd.to_numeric(satisfaction, errors="coerce")

    # 费用只对已处理/处理中的维修类工单产生，取消工单归零。
    result[WorkOrderColumn.AMOUNT.value] = _derive_amount(
        ticket_ids=result[WorkOrderColumn.TICKET_ID.value],
        repair_types=result[WorkOrderColumn.REPAIR_TYPE.value],
        statuses=result[WorkOrderColumn.STATUS.value],
    )

    # 丢弃登记时间无效的行，并按请求区间和项目做最终兜底过滤。
    result = result.loc[valid_time_mask].copy()
    start_at = pd.Timestamp(time_range.start_date)
    end_exclusive = pd.Timestamp(time_range.end_date) + pd.Timedelta(days=1)
    result = result.loc[
        (result[WorkOrderColumn.RECEIVED_AT.value] >= start_at)
        & (result[WorkOrderColumn.RECEIVED_AT.value] < end_exclusive)
    ]

    if property_name:
        project_mask = (
            result[WorkOrderColumn.PROJECT.value]
            .astype("string")
            .str.strip()
            .eq(property_name.strip())
            .fillna(False)
        )
        result = result.loc[project_mask]

    return result.sort_values(WorkOrderColumn.RECEIVED_AT.value).reset_index(drop=True)


def _clean_text(frame: pd.DataFrame, column: str) -> pd.Series:
    """读取文本列并去除首尾空白；缺失列返回全空字符串。"""
    if column not in frame.columns:
        return pd.Series("", index=frame.index, dtype="string")
    return frame[column].astype("string").fillna("").str.strip()


def _derive_repair_type(frame: pd.DataFrame) -> pd.Series:
    """从「工单类型」与「服务内容」推导看板使用的报修大类。

    合并为 5 类，避免饼图出现过长的长尾：
    安防告警、工程维修、住宅报修、客户服务、其他服务。
    """
    ticket_type = _clean_text(frame, "工单类型")
    service_path = _clean_text(frame, "工单服务项目") + " " + _clean_text(frame, "服务内容")

    repair_type = pd.Series("其他服务", index=frame.index, dtype="string")
    repair_type.loc[ticket_type.eq("数智中枢告警工单")] = "安防告警"
    repair_type.loc[ticket_type.eq("住宅报单")] = "住宅报修"
    repair_type.loc[ticket_type.isin(["客户报事报修", "客户投诉建议"])] = "客户服务"

    # 普通提交工单和补单中，只有明确勾选工程维修的才归入工程维修。
    engineering_mask = ticket_type.isin(["提交工单", "补单"]) & service_path.str.contains(
        "工程维修", na=False
    )
    repair_type.loc[engineering_mask] = "工程维修"
    return repair_type.fillna("其他服务")


def _derive_status(frame: pd.DataFrame) -> pd.Series:
    """把原始处理状态映射为平台统一状态。"""
    raw_status = _clean_text(frame, "处理状态")
    return raw_status.map(STATUS_MAPPING).fillna("处理中")


def _derive_owner(frame: pd.DataFrame) -> pd.Series:
    """按维修人员 -> 接单人 -> 登记人的优先级确定负责人。"""
    owner = _clean_text(frame, "维修人员")
    for fallback_column in ("接单人", "登记人"):
        owner = owner.mask(owner.eq(""), _clean_text(frame, fallback_column))
    return owner.replace("", "未指派")


def _derive_amount(
    *,
    ticket_ids: pd.Series,
    repair_types: pd.Series,
    statuses: pd.Series,
) -> pd.Series:
    """基于单号生成稳定、可复现的模拟费用。

    真实报表不含费用列；这里用哈希值代替随机数，保证相同工单在多次
    查询中的金额一致，便于前端看板对比。
    """
    amounts: list[float] = []
    for ticket_id, repair_type, status in zip(ticket_ids, repair_types, statuses):
        if status == "已取消":
            amounts.append(0.0)
            continue

        unit = _stable_unit(str(ticket_id), "amount")
        if repair_type == "安防告警":
            amounts.append(0.0)
        elif repair_type == "工程维修":
            amounts.append(round(80.0 + unit * 2800.0, 2))
        elif repair_type == "住宅报修":
            amounts.append(round(50.0 + unit * 1200.0, 2))
        elif repair_type == "客户服务":
            amounts.append(round(unit * 300.0, 2))
        else:
            amounts.append(round(unit * 500.0, 2))
    return pd.Series(amounts, index=ticket_ids.index, dtype="float64")


def _stable_unit(value: str, salt: str) -> float:
    """把字符串映射到 [0, 1) 的稳定小数。"""
    digest = hashlib.blake2s(f"{salt}:{value}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64
