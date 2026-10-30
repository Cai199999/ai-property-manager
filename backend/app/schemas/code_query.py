"""生成式 Pandas 查询的请求与响应契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.schemas.assistant import ParserInfo, TimeRange


class CodeQueryRequest(BaseModel):
    """自然语言代码查询请求。

    BYOK 字段均为可选：
    - provider_type 留空时使用 DEFAULT_LLM_PROVIDER（默认 deepseek）；
    - user_api_key 留空时回退系统 .env 中对应 Provider 的 Key；
    - model_name 留空时使用系统配置的默认模型。
    """

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
    provider_type: str | None = Field(
        default=None,
        max_length=32,
        description="大模型服务商：deepseek / doubao / qwen；留空使用系统默认",
    )
    user_api_key: SecretStr | None = Field(
        default=None,
        max_length=512,
        description="用户自带 API Key（BYOK）；留空回退系统 Key，服务端不会回显",
    )
    model_name: str | None = Field(
        default=None,
        max_length=128,
        description="模型名称；留空使用该 Provider 的默认模型",
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


class LLMInfo(BaseModel):
    """本次请求实际使用的大模型元信息，不含任何凭据。"""

    provider_type: str
    model: str
    key_source: Literal["user", "system", "missing"] = "system"


class CodeQueryMeta(BaseModel):
    """代码查询执行元信息。"""

    fetched_at: datetime
    elapsed_ms: int
    source_row_count: int
    source_file: str | None = None
    primary_table: str | None = None
    tables: list[QueryTableInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # 让调用方确认 BYOK 参数确实生效；只展示 provider/model/key 来源。
    llm: LLMInfo | None = None


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


class ActionResultResponse(BaseModel):
    """Agent 工具调用结果，不包含图表或查询结果表。

    前端看到 ``type == "action_result"`` 时不应走图表渲染分支。
    """

    request_id: str
    type: Literal["action_result"] = "action_result"
    action: Literal["delete_table"] = "delete_table"
    success: bool = True
    message: str
    deleted_table: str | None = None
    remaining_tables: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
