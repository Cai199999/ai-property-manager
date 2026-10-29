"""助手 API 的请求/响应契约。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantQueryRequest(BaseModel):
    """自然语言查询请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(
        min_length=2, max_length=500, description="自然语言查询，例如：汇总上个月工单"
    )
    property_name: str | None = Field(
        default=None,
        max_length=100,
        description="可选的项目名称过滤条件；留空表示全部项目",
    )


class TimeRange(BaseModel):
    """闭区间日期范围，起止日期均包含在查询结果内。"""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> "TimeRange":
        if self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return self


class GroupSummary(BaseModel):
    """按维度聚合后的一组统计值。"""

    name: str
    count: int
    amount: float


class WorkOrderSummary(BaseModel):
    """Pandas 汇总结果。"""

    total_records: int
    total_amount: float
    completed_count: int
    completion_rate: float = Field(ge=0.0, le=1.0)
    average_satisfaction: float | None
    by_project: list[GroupSummary]
    by_repair_type: list[GroupSummary]
    by_status: list[GroupSummary]


class ParserInfo(BaseModel):
    """时间范围解析元信息。"""

    engine: Literal["deepseek", "rule_based"]
    model: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class QueryMeta(BaseModel):
    """一次查询的执行元信息。"""

    fetched_at: datetime
    elapsed_ms: int
    row_count: int
    source_file: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AssistantQueryResponse(BaseModel):
    """统一查询结果。Java 网关直接透传此结构。"""

    request_id: str
    query: str
    data_source: str
    time_range: TimeRange
    parser: ParserInfo
    summary: WorkOrderSummary
    records_preview: list[dict[str, Any]]
    meta: QueryMeta


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    request_id: str
    error: ErrorDetail
