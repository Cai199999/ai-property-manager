"""Excel 原始内容的分页预览。

预览只输出磁盘文件中真实存在的列。费用金额等分析派生字段由
``TableDataset.derived_columns`` 标记，不伪装成用户上传的 Excel 内容。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.services.table_catalog import TableDataset


def build_table_preview(
    dataset: TableDataset,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """按行偏移截取一张表，并转换为可安全 JSON 序列化的结构。"""
    derived = set(dataset.derived_columns)
    columns = [
        {
            "name": column.name,
            "dtype": column.dtype,
            "null_count": column.null_count,
        }
        for column in dataset.columns
        if column.name not in derived
    ]
    column_names = [column["name"] for column in columns]

    total_rows = dataset.row_count
    # offset 超过总行数时返回空 rows，前端仍能展示表头。
    window = dataset.frame.iloc[offset : offset + limit]
    rows = [
        {name: _json_cell(record.get(name)) for name in column_names}
        for record in window.to_dict(orient="records")
    ]

    return {
        "name": dataset.name,
        "file_name": dataset.file_name,
        "role": dataset.role,
        "schema_group": dataset.schema_group,
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total_rows,
        "columns": columns,
        "rows": rows,
        "derived_columns": list(dataset.derived_columns),
    }


def _json_cell(value: Any) -> Any:
    """把 pandas/numpy 标量转换成 JSON 原生类型。

    Excel 空单元格会变成 NaN/NaT，直接写入 JSON 会产生非法字面量。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # 数组或复杂对象不参与单元格预览，交给后面的兜底转换。
        pass

    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
