"""多表目录与文件导入的响应契约。"""

from __future__ import annotations

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
