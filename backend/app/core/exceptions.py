"""统一业务异常。

异常中只放可安全返回给调用方的信息，不包含密钥、堆栈或本机绝对路径。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可预期业务异常的基类。"""

    code = "INTERNAL_ERROR"
    status_code = 500
    message = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)


class TimeRangeParseError(AppError):
    """自然语言中的时间范围无法识别，或超出允许范围。"""

    code = "TIME_RANGE_INVALID"
    status_code = 422
    message = "无法从请求中识别有效的数据时间范围"


class LLMServiceError(AppError):
    """大模型服务不可用或返回了无法解析的内容。"""

    code = "LLM_SERVICE_ERROR"
    status_code = 502
    message = "时间语义解析服务暂时不可用"


class DataSourceNotConfiguredError(AppError):
    """数据源适配器尚未接入。"""

    code = "DATA_SOURCE_NOT_CONFIGURED"
    status_code = 501
    message = "目标数据源适配器尚未配置"


class DataFetchError(AppError):
    """数据抓取或读取阶段失败。"""

    code = "DATA_FETCH_FAILED"
    status_code = 502
    message = "数据抓取失败，请稍后重试"


class DataContractError(AppError):
    """上游数据缺少约定列，无法安全汇总。"""

    code = "DATA_CONTRACT_INVALID"
    status_code = 502
    message = "上游数据格式不符合约定"


class CodeGenerationError(AppError):
    """模型多次尝试后仍无法生成可执行的数据分析代码。"""

    code = "CODE_GENERATION_FAILED"
    status_code = 422
    message = "无法生成可执行的数据分析代码"


class CodeExecutionTimeoutError(AppError):
    """生成代码在沙箱中执行超时。"""

    code = "CODE_EXECUTION_TIMEOUT"
    status_code = 504
    message = "生成代码执行超时"


class CodeSandboxUnavailableError(AppError):
    """运行环境缺少可用的隔离沙箱，拒绝执行生成代码。"""

    code = "CODE_SANDBOX_UNAVAILABLE"
    status_code = 503
    message = "代码执行沙箱不可用，已拒绝执行"


class TableImportError(AppError):
    """上传文件不合法或无法解析为数据表。"""

    code = "TABLE_IMPORT_FAILED"
    status_code = 422
    message = "文件导入失败"


class TableImportUnavailableError(AppError):
    """多表目录未启用，拒绝上传。"""

    code = "TABLE_IMPORT_UNAVAILABLE"
    status_code = 503
    message = "多表数据目录未启用，无法导入文件"


class ReportNameInvalidError(AppError):
    """下载文件名不符合导出文件名规则。"""

    code = "REPORT_NAME_INVALID"
    status_code = 400
    message = "下载文件名不合法"


class ReportNotFoundError(AppError):
    """导出文件不存在、已过期或已被清理。"""

    code = "REPORT_NOT_FOUND"
    status_code = 404
    message = "下载文件不存在或已过期"
