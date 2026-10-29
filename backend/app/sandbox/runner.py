"""在隔离子进程中执行模型生成的 Pandas 代码。

本文件不导入项目内任何模块，避免把父进程配置或密钥带进沙箱。
父进程通过 stdin 传入 JSON，结果只通过 stdout 返回。
"""

from __future__ import annotations

import builtins
import json
import math
import re
import resource
import sys
import traceback
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

_SAFE_BUILTIN_NAMES = (
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "filter",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "map",
    "max",
    "min",
    "range",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "ArithmeticError",
    "Exception",
    "IndexError",
    "KeyError",
    "RuntimeError",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
)
_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in _SAFE_BUILTIN_NAMES
    if hasattr(builtins, name)
}


class _DiscardWriter:
    """吞掉生成代码的输出，防止 print 污染 JSON 协议或打爆内存。"""

    def write(self, _value: str) -> int:
        return len(_value)

    def flush(self) -> None:
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = _execute(payload)
    except BaseException as exc:  # noqa: BLE001 - 沙箱边界必须兜住所有异常
        result = _error_payload(exc)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def _execute(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    limits = payload.get("limits") or {}
    if not isinstance(code, str) or not code.strip():
        raise ValueError("生成的代码为空")

    frames_payload = payload.get("frames")
    if frames_payload is None and isinstance(payload.get("frame"), dict):
        # 兼容旧的单表协议。
        frames_payload = {"source_df": payload["frame"]}
    if not isinstance(frames_payload, dict) or not frames_payload:
        raise ValueError("缺少数据表")

    max_tables = int(limits.get("max_tables", 50))
    max_frame_rows = int(limits.get("max_frame_rows", 50_000))
    max_frame_columns = int(limits.get("max_frame_columns", 100))
    if len(frames_payload) > max_tables:
        raise ValueError(f"数据表数量超过上限 {max_tables}")

    frames: dict[str, pd.DataFrame] = {}
    for name, frame_payload in frames_payload.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("数据表名不合法")
        if not isinstance(frame_payload, dict):
            raise ValueError(f"数据表 {name} 结构不合法")
        frame = _restore_frame(frame_payload)
        if len(frame) > max_frame_rows:
            raise ValueError(f"数据表 {name} 行数超过上限 {max_frame_rows}")
        if len(frame.columns) > max_frame_columns:
            raise ValueError(f"数据表 {name} 列数超过上限 {max_frame_columns}")
        frames[name] = frame

    primary_table = payload.get("primary_table")
    if not isinstance(primary_table, str) or primary_table not in frames:
        primary_table = next(iter(frames))

    _apply_resource_limits()

    namespace = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "np": np,
        "tables": {name: frame.copy() for name, frame in frames.items()},
        "source_df": frames[primary_table].copy(),
        "result_df": None,
    }

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sink = _DiscardWriter()
    try:
        sys.stdout = sink
        sys.stderr = sink
        compiled = compile(code, "<generated>", "exec")
        exec(compiled, namespace)  # noqa: S102 - 仅发生在隔离子进程
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr

    result_df = namespace.get("result_df")
    if not isinstance(result_df, pd.DataFrame):
        raise TypeError(
            "result_df 必须是 pandas DataFrame，"
            f"当前类型是 {type(result_df).__name__}"
        )

    max_rows = int(limits.get("max_rows", 5_000))
    max_columns = int(limits.get("max_columns", 50))
    max_cell_chars = int(limits.get("max_cell_chars", 1_000))
    max_output_bytes = int(limits.get("max_output_bytes", 5_000_000))

    if len(result_df) > max_rows:
        raise ValueError(f"结果行数 {len(result_df)} 超过上限 {max_rows}")
    if len(result_df.columns) > max_columns:
        raise ValueError(f"结果列数 {len(result_df.columns)} 超过上限 {max_columns}")

    column_names = [str(column) for column in result_df.columns]
    if len(set(column_names)) != len(column_names):
        raise ValueError("结果表存在重复列名，请先聚合或重命名")

    warnings: list[str] = []
    rows = [
        {
            column_names[index]: _json_safe(
                value,
                max_cell_chars=max_cell_chars,
                warnings=warnings,
            )
            for index, value in enumerate(row)
        }
        for row in result_df.itertuples(index=False, name=None)
    ]

    result = {
        "ok": True,
        "columns": [
            {"name": name, "dtype": str(result_df.iloc[:, index].dtype)}
            for index, name in enumerate(column_names)
        ],
        "rows": rows,
        "row_count": len(rows),
        "warnings": warnings,
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > max_output_bytes:
        raise ValueError("结果数据过大，请先聚合或减少返回列")
    return result


def _restore_frame(payload: dict[str, Any]) -> pd.DataFrame:
    columns = [str(column) for column in payload.get("columns") or []]
    dtypes = [str(dtype) for dtype in payload.get("dtypes") or []]
    values = payload.get("values") or []
    if not columns or len(columns) != len(dtypes):
        raise ValueError("source_df 列结构不完整")

    frame = pd.DataFrame(values, columns=columns)
    for index, dtype in enumerate(dtypes):
        column = frame.columns[index]
        if dtype.startswith("datetime64"):
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
        elif dtype.startswith(("int", "uint")):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        elif dtype.startswith("float"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        elif dtype.startswith("bool"):
            frame[column] = frame[column].astype("boolean")
        else:
            frame[column] = frame[column].astype("string")
    return frame


def _apply_resource_limits() -> None:
    """限制 CPU、地址空间、文件大小和进程数，防止资源耗尽。"""
    limits = (
        (resource.RLIMIT_CPU, (5, 5)),
        (resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000)),
        (resource.RLIMIT_FSIZE, (1_000_000, 1_000_000)),
        (resource.RLIMIT_NOFILE, (64, 64)),
        (resource.RLIMIT_NPROC, (64, 64)),
        (resource.RLIMIT_CORE, (0, 0)),
    )
    for resource_id, value in limits:
        try:
            resource.setrlimit(resource_id, value)
        except (ValueError, OSError):
            # 某些平台不支持个别 limit；父进程超时仍然是最终兜底。
            continue


def _json_safe(value: Any, *, max_cell_chars: int, warnings: list[str]) -> Any:
    """把 Pandas/NumPy 标量转换成严格 JSON 可序列化的值。"""
    if value is None:
        return None

    # NaT/NA/NaN 必须最先判断，否则 NaT 会被当成 datetime 输出 "NaT"。
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, bool) and missing:
        return None

    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, (np.integer, np.floating, np.bool_)):
        value = value.item()
    elif isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (int, bool)):
        return value
    if isinstance(value, str):
        if len(value) > max_cell_chars:
            warnings.append("部分文本单元格已截断")
            return value[: max_cell_chars - 1] + "…"
        return value

    raise TypeError(f"结果单元格类型不支持: {type(value).__name__}")


def _error_payload(exc: BaseException) -> dict[str, Any]:
    line = _generated_line(exc)
    message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(exc)).strip()
    if len(message) > 2_000:
        message = message[:1_999] + "…"
    return {
        "ok": False,
        "error_type": type(exc).__name__,
        "message": message,
        "line": line,
    }


def _generated_line(exc: BaseException) -> int | None:
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == "<generated>":
            return frame.lineno
    return None


if __name__ == "__main__":
    raise SystemExit(main())
