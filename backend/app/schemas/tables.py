"""多表目录与文件导入的响应契约。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TableColumnInfo(BaseModel):
    """对外暴露的列摘要，不包含数据样例。"""

    name: str
    dtype: str
    null_count: int


class TableInfo(BaseModel):
    """一张已扫描到的数据表。"""

    name: str
    role: str
    description: str = ""
    schema_group: str = ""
    file_name: str
    sheet_name: str
    row_count: int
    column_count: int
    primary_key: list[str] = Field(default_factory=list)
    time_column: str | None = None
    columns: list[TableColumnInfo] = Field(default_factory=list)


class TableCatalogResponse(BaseModel):
    """当前目录中的全部数据表。"""

    tables: list[TableInfo] = Field(default_factory=list)
    primary_table: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TableUploadResponse(TableCatalogResponse):
    """上传成功后的结果：新表 + 刷新后的目录。"""

    imported: TableInfo | None = None
    imported_tables: list[TableInfo] = Field(default_factory=list)


class TablePreviewResponse(BaseModel):
    """Excel 原始内容的分页预览。

    ``columns`` 只包含磁盘文件中的真实列；``derived_columns`` 是分析阶段
    在内存中补充的字段（例如费用金额），不会伪装成 Excel 内容。
    """

    name: str
    file_name: str
    role: str
    schema_group: str = ""
    total_rows: int
    offset: int
    limit: int
    has_more: bool
    columns: list[TableColumnInfo] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    derived_columns: list[str] = Field(default_factory=list)
