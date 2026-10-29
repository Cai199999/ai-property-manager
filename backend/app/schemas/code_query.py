"""生成式 Pandas 查询的请求与响应契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.assistant import ParserInfo, TimeRange


class CodeQueryRequest(BaseModel):
    """自然语言代码查询请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(
        min_length=2,
        max_length=500,
        description="自然语言查询，例如：按项目统计上个月工单量和费用",
    )
    property_name: str | None = Field(
        default=None,
        max_length=100,
        description="可选的项目名称过滤条件；留空表示全部项目",
    )


class ResultColumn(BaseModel):
    """结果表的一列。"""

    name: str
    dtype: str


class CodeResult(BaseModel):
    """生成代码返回的通用二维结果，不再绑定固定业务 Schema。"""

    columns: list[ResultColumn]
    rows: list[dict[str, Any]]
    row_count: int


class CodeExecutionInfo(BaseModel):
    """代码生成与重试过程的可见信息。"""

    attempts: int
    generated_code: str | None = None
    retry_errors: list[str] = Field(default_factory=list)


class ChartSeries(BaseModel):
    """图表中的一条数值序列。"""

    name: str
    key: str
    unit: str | None = None


class ChartMeta(BaseModel):
    """前端动态渲染所需的图表描述，不绑定具体业务字段。"""

    type: Literal["bar", "line", "pie", "table"] = "table"
    title: str = "查询结果"
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)


class QueryTableInfo(BaseModel):
    """本次查询可用的数据表摘要，不暴露服务器路径。"""

    name: str
    role: str
    row_count: int
    file_name: str | None = None


class CodeQueryMeta(BaseModel):
    """代码查询执行元信息。"""

    fetched_at: datetime
    elapsed_ms: int
    source_row_count: int
    source_file: str | None = None
    primary_table: str | None = None
    tables: list[QueryTableInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CodeQueryResponse(BaseModel):
    """代码查询响应。Java 网关以 JsonNode 原样透传，不绑定业务字段。"""

    request_id: str
    query: str
    data_source: str
    time_range: TimeRange
    parser: ParserInfo
    result: CodeResult
    chart_meta: ChartMeta = Field(default_factory=ChartMeta)
    download_url: str | None = None
    word_download_url: str | None = None
    code: CodeExecutionInfo
    meta: CodeQueryMeta
