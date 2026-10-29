"""根据通用结果表推断前端可渲染的图表元数据。

模型只负责生成 Pandas 代码，展示配置由服务端从 ``result_df`` 的列类型和
数据形态确定性推断，避免再引入一轮不受控的模型输出。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from app.schemas.code_query import ChartMeta, ChartSeries

_NUMERIC_DTYPE_MARKERS = ("int", "float", "decimal", "number")
_DATETIME_DTYPE_MARKERS = ("datetime", "date")
_DATE_VALUE_RE = re.compile(r"^\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?")
_DATE_COLUMN_MARKERS = ("日期", "时间", "年月", "月份", "年度", "周")
_UNIT_RULES = (
    ("金额", "元"),
    ("费用", "元"),
    ("成本", "元"),
    ("率", "%"),
    ("占比", "%"),
    ("数量", "个"),
    ("工单", "单"),
    ("次数", "次"),
)
# 用户在提问里显式点名的图表类型优先于启发式推断。
_CHART_KEYWORD_TO_TYPE: dict[str, Literal["bar", "line", "pie"]] = {
    "柱状图": "bar",
    "柱形图": "bar",
    "条形图": "bar",
    "直方图": "bar",
    "饼图": "pie",
    "饼状图": "pie",
    "环形图": "pie",
    "折线图": "line",
    "趋势图": "line",
    "走势图": "line",
}
# “请用折线图”这类祈使句比“先看饼图还是折线图”更明确，优先匹配指令动词。
_CHART_INSTRUCTION_RE = re.compile(
    "(?:请用|使用|采用|改用|改为|换成|画成|用|以)"
    "(柱状图|柱形图|条形图|直方图|饼状图|饼图|环形图|折线图|趋势图|走势图)"
)


def infer_chart_meta(
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    *,
    question: str | None = None,
) -> ChartMeta:
    """从结果列和样例行推断柱状图/折线图/饼图/纯表格。

    ``question`` 用于识别用户显式要求的图表类型（如“用柱状图展示”）；
    这类要求只在数据形态支持时才生效，避免出现无法渲染的图表。
    """
    names = [str(column.get("name", "")) for column in columns]
    if not names:
        return ChartMeta(type="table", title="查询结果", x=None, y=[], series=[])

    numeric = [
        name
        for name, column in zip(names, columns)
        if _is_numeric_dtype(str(column.get("dtype", "")))
    ]
    categories = [name for name in names if name not in numeric and _has_values(rows, name)]

    if not numeric or not categories:
        return ChartMeta(type="table", title="查询结果", x=None, y=[], series=[])

    x_name = categories[0]
    y_names = numeric[:3]
    chart_type: Literal["bar", "line", "pie", "table"] = "bar"

    if _looks_like_time_series(x_name, rows):
        chart_type = "line"
    elif len(y_names) == 1 and _distinct_count(rows, x_name) <= 6:
        chart_type = "pie"

    requested = _requested_chart_type(question)
    # 饼图只能表达单一数值列，多列时退回柱状图。
    if requested and not (requested == "pie" and len(y_names) > 1):
        chart_type = requested

    series = [ChartSeries(name=name, key=name, unit=_infer_unit(name)) for name in y_names]
    return ChartMeta(
        type=chart_type,
        title=f"{y_names[0]}按{x_name}",
        x=x_name,
        y=y_names,
        series=series,
    )


def _requested_chart_type(question: str | None) -> Literal["bar", "line", "pie"] | None:
    """识别提问中显式点名的图表类型。

    先找“请用/使用/改成……柱状图”这类指令；没有指令时退回第一个出现的关键词。
    """
    if not question:
        return None
    instruction = _CHART_INSTRUCTION_RE.search(question)
    if instruction:
        return _CHART_KEYWORD_TO_TYPE[instruction.group(1)]
    earliest: tuple[int, Literal["bar", "line", "pie"]] | None = None
    for keyword, chart_type in _CHART_KEYWORD_TO_TYPE.items():
        position = question.find(keyword)
        if position < 0:
            continue
        if earliest is None or position < earliest[0]:
            earliest = (position, chart_type)
    return earliest[1] if earliest else None


def _is_numeric_dtype(dtype: str) -> bool:
    normalized = dtype.lower()
    if "bool" in normalized:
        return False
    return any(marker in normalized for marker in _NUMERIC_DTYPE_MARKERS)


def _has_values(rows: list[dict[str, Any]], name: str) -> bool:
    return any(row.get(name) not in (None, "") for row in rows)


def _looks_like_time_series(name: str, rows: list[dict[str, Any]]) -> bool:
    if any(marker in name for marker in _DATE_COLUMN_MARKERS):
        return True
    for row in rows:
        value = row.get(name)
        if value is None:
            continue
        return bool(_DATE_VALUE_RE.match(str(value)))
    return False


def _distinct_count(rows: list[dict[str, Any]], name: str) -> int:
    return len({str(row.get(name)) for row in rows if row.get(name) is not None})


def _infer_unit(name: str) -> str | None:
    for marker, unit in _UNIT_RULES:
        if marker in name:
            return unit
    return None
