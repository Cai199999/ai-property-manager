"""日志初始化与密钥脱敏。"""

from __future__ import annotations

import logging
import re

# 兜底脱敏规则：即使将来有人在日志里打印了请求体或请求头，
# 也不能把用户自带的 API Key 原样写进日志文件。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|auth[_-]?token|"
            r"user[_-]?api[_-]?key|password)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"
        ),
        r"\1***",
    ),
)


def redact_secrets(text: str) -> str:
    """把文本中的常见密钥形态替换为固定掩码。"""
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SecretRedactionFilter(logging.Filter):
    """挂在日志 handler 上的最后一道脱敏防线。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志格式化失败不能影响主流程
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction() -> None:
    """给当前已注册的 handler 安装脱敏过滤器。

    uvicorn 会先于应用初始化日志，因此不能只处理 root logger；
    这里遍历所有已存在的 logger，确保启动后新增的 handler 也覆盖到。
    """
    redaction = SecretRedactionFilter()
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
                handler.addFilter(redaction)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_secret_redaction()
