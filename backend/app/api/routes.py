"""助手对外 API。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request

from app.schemas.assistant import AssistantQueryRequest, AssistantQueryResponse

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


def get_request_id(request: Request) -> str:
    """优先复用网关传入的关联 ID，非法值则重新生成。"""
    candidate = request.headers.get("X-Request-Id", "").strip()
    if candidate and len(candidate) <= 128:
        return candidate
    return str(uuid.uuid4())


@router.post(
    "/query",
    response_model=AssistantQueryResponse,
    summary="自然语言查询物业工单并汇总",
)
async def query_assistant(
    payload: AssistantQueryRequest,
    request: Request,
) -> AssistantQueryResponse:
    return await request.app.state.assistant_agent.handle(
        message=payload.message,
        request_id=get_request_id(request),
        property_name=payload.property_name,
    )


@router.get("/sources", summary="查看已注册的数据源")
async def list_sources(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    registry = request.app.state.fetcher_registry
    return {
        "active": settings.data_source,
        "available": registry.available_sources,
    }
