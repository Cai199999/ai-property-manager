"""助手对外 API。"""

from __future__ import annotations

import uuid
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.exceptions import (
    ReportNotFoundError,
    TableImportError,
    TableImportUnavailableError,
    TableNotFoundError,
    TablePreviewUnavailableError,
)
from app.schemas.assistant import AssistantQueryRequest, AssistantQueryResponse
from app.schemas.code_query import CodeQueryRequest, CodeQueryResponse
from app.schemas.tables import (
    TableCatalogResponse,
    TableColumnInfo,
    TableInfo,
    TablePreviewResponse,
    TableUploadResponse,
)
from app.services.report_export import media_type_for
from app.services.table_catalog import TableDataset, TableSnapshot
from app.services.table_preview import build_table_preview

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
    summary="下载查询结果文件（Excel / Word）",
    response_class=FileResponse,
)
async def download_report(filename: str, request: Request) -> FileResponse:
    exporter = getattr(request.app.state, "report_exporter", None)
    if exporter is None or not exporter.enabled:
        raise ReportNotFoundError()

    path = await anyio.to_thread.run_sync(partial(exporter.resolve, filename))
    return FileResponse(
        path,
        media_type=media_type_for(filename),
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


@router.get(
    "/tables/preview",
    response_model=TablePreviewResponse,
    summary="分页预览 Excel 原始内容",
)
async def preview_table(
    request: Request,
    name: str = Query(..., min_length=1, max_length=255),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> TablePreviewResponse:
    catalog = getattr(request.app.state, "table_catalog", None)
    if catalog is None or not request.app.state.settings.table_directory_enabled:
        raise TablePreviewUnavailableError()

    snapshot = await anyio.to_thread.run_sync(catalog.scan)
    dataset = snapshot.table_map().get(name)
    if dataset is None:
        raise TableNotFoundError()

    preview = build_table_preview(dataset, offset=offset, limit=limit)
    return TablePreviewResponse(**preview)


@router.post(
    "/tables/upload",
    response_model=TableUploadResponse,
    summary="上传 Excel 并加入多表目录",
)
async def upload_table(
    request: Request,
    file: list[UploadFile] = File(
        ...,
        description="一个或多个 .xlsx 文件，可重复提交 file 字段",
    ),
    filename: list[str] | None = Form(
        default=None,
        description="与 file 顺序对应的原始文件名；网关用于透传中文名",
    ),
) -> TableUploadResponse:
    service = getattr(request.app.state, "table_import_service", None)
    if service is None or not service.enabled:
        raise TableImportUnavailableError()

    uploads = [item for item in file if item is not None]
    if not uploads:
        raise TableImportError("请选择至少一个 .xlsx 文件")

    settings = request.app.state.settings
    if len(uploads) > settings.table_upload_max_files:
        raise TableImportError(f"单次最多导入 {settings.table_upload_max_files} 个文件")

    provided_names = [name.strip() for name in (filename or []) if name.strip()]
    max_bytes = settings.table_upload_max_bytes
    imported: list[TableDataset] = []
    failures: list[str] = []
    snapshot: TableSnapshot | None = None

    for index, upload in enumerate(uploads):
        # 网关会把原始文件名放在独立字段中，避免 multipart 头部的编码差异；
        # 直接调用本接口（curl/Swagger）时仍回退到文件段自带的 filename。
        original_name = (
            provided_names[index] if index < len(provided_names) else (upload.filename or "")
        ).strip()
        display_name = original_name or f"第 {index + 1} 个文件"
        if len(original_name) > 255:
            await upload.close()
            failures.append(f"{display_name[:40]}…: 文件名过长")
            continue

        try:
            content = await _read_upload(upload, max_bytes=max_bytes)
            dataset, snapshot = await anyio.to_thread.run_sync(
                partial(
                    service.import_bytes,
                    filename=original_name,
                    content=content,
                )
            )
        except TableImportError as exc:
            failures.append(f"{display_name}: {exc.message}")
            continue
        imported.append(dataset)

    if not imported or snapshot is None:
        message = failures[0] if failures else "文件导入失败"
        raise TableImportError(message, details={"failures": failures})

    imported_infos = [_table_info(dataset) for dataset in imported]
    catalog_response = _catalog_response(snapshot)
    return TableUploadResponse(
        imported=imported_infos[0],
        imported_tables=imported_infos,
        tables=catalog_response.tables,
        primary_table=catalog_response.primary_table,
        warnings=list(catalog_response.warnings) + failures,
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
        schema_group=dataset.schema_group,
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
