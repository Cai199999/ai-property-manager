"""统一异常响应。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError
from app.schemas.assistant import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _error_response(
    *,
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        request_id=request_id,
        error=ErrorDetail(code=code, message=message, details=details or {}),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = _request_id(request)
        logger.warning(
            "业务异常 request_id=%s code=%s status=%s message=%s",
            request_id,
            exc.code,
            exc.status_code,
            exc.message,
        )
        return _error_response(
            request_id=request_id,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = _request_id(request)
        errors = [
            {
                "location": ".".join(str(part) for part in error.get("loc", ())),
                "message": str(error.get("msg", "参数校验失败")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
        return _error_response(
            request_id=request_id,
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="请求参数校验失败",
            details={"errors": errors},
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.exception("未处理异常 request_id=%s", request_id, exc_info=exc)
        return _error_response(
            request_id=request_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="服务内部错误",
            headers={"X-Request-Id": request_id},
        )
