"""助手对外 API。"""

from __future__ import annotations

import uuid
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.exceptions import (
    ReportNotFoundError,
    TableImportError,
    TableImportUnavailableError,
)
from app.schemas.assistant import AssistantQueryRequest, AssistantQueryResponse
from app.schemas.code_query import CodeQueryRequest, CodeQueryResponse
from app.schemas.tables import (
    TableCatalogResponse,
    TableColumnInfo,
    TableInfo,
    TableUploadResponse,
)
from app.services.report_export import REPORT_MEDIA_TYPE
from app.services.table_catalog import TableDataset, TableSnapshot

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


@router.post(
    "/query/code",
    response_model=CodeQueryResponse,
    summary="自然语言生成 Pandas 代码并返回通用结果表",
)
async def query_with_generated_code(
    payload: CodeQueryRequest,
    request: Request,
) -> CodeQueryResponse:
    return await request.app.state.code_query_agent.handle(
        message=payload.message,
        request_id=get_request_id(request),
        property_name=payload.property_name,
    )


@router.get(
    "/download/{filename}",
    summary="下载查询结果 Excel",
    response_class=FileResponse,
)
async def download_report(filename: str, request: Request) -> FileResponse:
    exporter = getattr(request.app.state, "report_exporter", None)
    if exporter is None or not exporter.enabled:
        raise ReportNotFoundError()

    path = await anyio.to_thread.run_sync(partial(exporter.resolve, filename))
    return FileResponse(
        path,
        media_type=REPORT_MEDIA_TYPE,
        filename=filename,
        headers={
            "Cache-Control": "private, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/sources", summary="查看已注册的数据源")
async def list_sources(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    registry = request.app.state.fetcher_registry
    return {
        "active": settings.data_source,
        "available": registry.available_sources,
    }


@router.get(
    "/tables",
    response_model=TableCatalogResponse,
    summary="查看当前多表目录",
)
async def list_tables(request: Request) -> TableCatalogResponse:
    catalog = getattr(request.app.state, "table_catalog", None)
    if catalog is None or not request.app.state.settings.table_directory_enabled:
        return TableCatalogResponse(
            tables=[],
            warnings=["多表数据目录未启用"],
        )

    snapshot = await anyio.to_thread.run_sync(catalog.scan)
    return _catalog_response(snapshot)


@router.post(
    "/tables/upload",
    response_model=TableUploadResponse,
    summary="上传 Excel 并加入多表目录",
)
async def upload_table(
    request: Request,
    file: UploadFile = File(..., description="仅支持 .xlsx 文件"),
    filename: str | None = Form(
        default=None,
        description="原始文件名；网关用独立字段透传中文名",
    ),
) -> TableUploadResponse:
    service = getattr(request.app.state, "table_import_service", None)
    if service is None or not service.enabled:
        raise TableImportUnavailableError()

    # 网关会把原始文件名放在独立字段中，避免 multipart 头部的编码差异；
    # 直接调用本接口（curl/Swagger）时仍回退到文件段自带的 filename。
    original_name = (filename or file.filename or "").strip()
    if len(original_name) > 255:
        raise TableImportError("文件名过长")

    max_bytes = request.app.state.settings.table_upload_max_bytes
    content = await _read_upload(file, max_bytes=max_bytes)
    dataset, snapshot = await anyio.to_thread.run_sync(
        partial(
            service.import_bytes,
            filename=original_name,
            content=content,
        )
    )
    catalog_response = _catalog_response(snapshot)
    return TableUploadResponse(
        imported=_table_info(dataset),
        **catalog_response.model_dump(),
    )


async def _read_upload(file: UploadFile, *, max_bytes: int) -> bytes:
    """分块读取上传内容，超限立即中断，避免把大文件全读进内存。"""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TableImportError(f"文件超过 {max_bytes / 1_000_000:g} MB 限制")
            chunks.append(chunk)
    finally:
        await file.close()
    return b"".join(chunks)


def _catalog_response(snapshot: TableSnapshot) -> TableCatalogResponse:
    return TableCatalogResponse(
        tables=[_table_info(table) for table in snapshot.tables],
        primary_table=snapshot.primary_table,
        warnings=list(snapshot.warnings),
    )


def _table_info(dataset: TableDataset) -> TableInfo:
    return TableInfo(
        name=dataset.name,
        role=dataset.role,
        description=dataset.description,
        file_name=dataset.file_name,
        sheet_name=dataset.sheet_name,
        row_count=dataset.row_count,
        column_count=dataset.column_count,
        primary_key=list(dataset.primary_key),
        time_column=dataset.time_column,
        columns=[
            TableColumnInfo(
                name=column.name,
                dtype=column.dtype,
                null_count=column.null_count,
            )
            for column in dataset.columns
        ],
    )
