"""FastAPI 应用入口。

本地运行：
    cd backend
    .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.adapters.base import DataFetcher
from app.adapters.registry import create_default_registry
from app.api.errors import register_exception_handlers
from app.api.routes import router
from app.config import Settings
from app.core.logging import configure_logging
from app.services.agent import AssistantAgent
from app.services.code_executor import CodeExecutor
from app.services.code_generator import LLMCodeGenerator
from app.services.code_query_agent import CodeQueryAgent
from app.services.deepseek_extractor import LLMTimeRangeExtractor
from app.services.llm_provider import LLMProviderFactory, SUPPORTED_PROVIDERS
from app.services.report_export import ExcelReportExporter
from app.services.word_exporter import LLMReportSummarizer, WordReportExporter
from app.services.table_catalog import TableCatalog
from app.services.table_import import TableImportService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    extractor: LLMTimeRangeExtractor | None = None,
    fetcher: DataFetcher | None = None,
    code_generator: LLMCodeGenerator | None = None,
    code_executor: CodeExecutor | None = None,
    table_catalog: TableCatalog | None = None,
    provider_factory: LLMProviderFactory | None = None,
) -> FastAPI:
    """应用工厂，便于测试注入替身而不污染全局依赖。"""
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)

    registry = create_default_registry()
    resolved_fetcher = fetcher or registry.create(
        resolved_settings.data_source,
        resolved_settings,
    )
    # 三个 LLM 组件共享同一个 Provider 工厂，从而复用连接池，
    # 并保证同一请求内 provider/model/key 的选择完全一致。
    resolved_providers = provider_factory or LLMProviderFactory(resolved_settings)
    resolved_extractor = extractor or LLMTimeRangeExtractor(
        resolved_settings,
        provider_factory=resolved_providers,
    )
    resolved_code_generator = code_generator or LLMCodeGenerator(
        resolved_settings,
        provider_factory=resolved_providers,
    )
    resolved_code_executor = code_executor or CodeExecutor(resolved_settings)
    resolved_table_catalog = table_catalog
    if resolved_table_catalog is None and resolved_settings.table_directory_enabled:
        resolved_table_catalog = TableCatalog(resolved_settings)
    table_import_service = (
        TableImportService(resolved_settings, resolved_table_catalog)
        if resolved_table_catalog is not None
        else None
    )
    report_exporter = ExcelReportExporter(resolved_settings)
    word_exporter = WordReportExporter(resolved_settings)
    report_summarizer = LLMReportSummarizer(
        resolved_settings,
        provider_factory=resolved_providers,
    )

    agent = AssistantAgent(resolved_settings, resolved_extractor, resolved_fetcher)
    code_agent = CodeQueryAgent(
        resolved_settings,
        resolved_extractor,
        resolved_fetcher,
        resolved_code_generator,
        resolved_code_executor,
        table_catalog=resolved_table_catalog,
        report_exporter=report_exporter,
        word_exporter=word_exporter,
        report_summarizer=report_summarizer,
        provider_factory=resolved_providers,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "%s v%s 启动 data_source=%s env=%s",
            resolved_settings.app_name,
            __version__,
            resolved_fetcher.source,
            resolved_settings.app_env,
        )
        yield
        # 共享工厂负责关闭连接池；各组件的 aclose 在共享模式下是空操作，
        # 但仍保留以兼容测试中注入的独立实例。
        await resolved_providers.aclose()
        await resolved_extractor.aclose()
        await report_summarizer.aclose()
        close_generator = getattr(resolved_code_generator, "aclose", None)
        if close_generator is not None:
            await close_generator()
        logger.info("应用已停止")

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        description="自然语言驱动的物业数据查询与汇总服务",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.assistant_agent = agent
    app.state.code_query_agent = code_agent
    app.state.fetcher_registry = registry
    app.state.table_catalog = resolved_table_catalog
    app.state.table_import_service = table_import_service
    app.state.report_exporter = report_exporter

    # 前端静态页运行在 8081，导入文件直传 8000，需要单独放行。
    if resolved_settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id"],
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        candidate = request.headers.get("X-Request-Id", "").strip()
        request_id = candidate if candidate and len(candidate) <= 128 else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    register_exception_handlers(app)
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": resolved_settings.app_name,
                "version": __version__,
                "docs": "/docs",
                "health": "/health",
            }
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "data_source": resolved_fetcher.source,
            "llm_configured": bool(resolved_settings.deepseek_key),
            "default_llm_provider": resolved_settings.default_llm_provider,
            "llm_providers": list(SUPPORTED_PROVIDERS),
            "code_query_enabled": resolved_settings.code_query_enabled,
            "code_sandbox_available": resolved_code_executor.available,
            "table_directory_enabled": resolved_settings.table_directory_enabled,
            "table_catalog_ready": resolved_table_catalog is not None,
            "report_export_enabled": resolved_settings.report_export_enabled,
        }

    return app


app = create_app()
