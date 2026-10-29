"""生成代码的提取与静态安全校验。

这里的目标不是把 Python 变成安全语言，而是在进入沙箱前尽早拒绝明显的
文件、网络、导入和对象逃逸代码。真正的隔离边界仍然是独立沙箱进程。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

_CODE_FENCE_RE = re.compile(
    r"```[ \t]*(?:(?:python|py)[ \t]*)?\r?\n(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
_RESULT_NAME = "result_df"

# 这些名称不应出现在生成代码里；需要的能力通过 pd/np/source_df 注入。
_DENY_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "open",
        "setattr",
        "delattr",
        "vars",
    }
)

# 重点拦截 Pandas/NumPy 的文件、网络和序列化入口。沙箱是最终边界，
# 这里做静态拦截可以减少危险代码真正进入执行阶段的机会。
_DENY_ATTRS = frozenset(
    {
        "ExcelFile",
        "ExcelWriter",
        "HDFStore",
        "compat",
        "core",
        "eval",
        "exec",
        "fork",
        "getenv",
        "io",
        "load",
        "loads",
        "memmap",
        "open",
        "popen",
        "read_clipboard",
        "read_csv",
        "read_excel",
        "read_feather",
        "read_fwf",
        "read_gbq",
        "read_hdf",
        "read_html",
        "read_json",
        "read_orc",
        "read_parquet",
        "read_pickle",
        "read_sas",
        "read_spss",
        "read_sql",
        "read_sql_query",
        "read_sql_table",
        "read_stata",
        "read_table",
        "read_xml",
        "save",
        "savez",
        "savez_compressed",
        "spawn",
        "system",
        "to_clipboard",
        "to_csv",
        "to_excel",
        "to_feather",
        "to_gbq",
        "to_hdf",
        "to_html",
        "to_json",
        "to_latex",
        "to_markdown",
        "to_orc",
        "to_parquet",
        "to_pickle",
        "to_sql",
        "to_stata",
        "to_xml",
        "testing",
        "urlopen",
        "util",
    }
)

_DENY_NODE_TYPES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
)


class CodeExtractionError(ValueError):
    """模型响应中无法提取出唯一、完整的 Python 代码。"""


class UnsafeCodeError(ValueError):
    """生成代码命中静态安全规则，不能进入执行阶段。"""


@dataclass(frozen=True, slots=True)
class ExtractedCode:
    """提取结果及可展示给用户的警告。"""

    code: str
    warnings: tuple[str, ...] = ()


def extract_python_code(content: str, *, max_chars: int) -> ExtractedCode:
    """从模型响应中提取 Python 代码。

    只处理边界处的 Markdown 围栏，不做“全局删除反引号”这类会破坏字符串
    字面量的操作。没有围栏时要求整个响应本身是完整代码。
    """
    if not isinstance(content, str) or not content.strip():
        raise CodeExtractionError("模型没有返回代码内容")

    text = content.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [block.strip() for block in _CODE_FENCE_RE.findall(text) if block.strip()]
    warnings: list[str] = []

    if len(blocks) == 1:
        code = blocks[0]
        warnings.append("已移除 Markdown 代码围栏")
    elif len(blocks) > 1:
        # 多个代码块时只接受唯一包含 result_df 的块，避免猜错结果。
        result_blocks = [block for block in blocks if _contains_result_assignment(block)]
        if len(result_blocks) != 1:
            raise CodeExtractionError("模型返回了多个代码块，无法确定最终结果代码")
        code = result_blocks[0]
        warnings.append("模型返回多个代码块，已选择包含 result_df 的唯一代码块")
    else:
        code = text

    if len(code) > max_chars:
        raise CodeExtractionError(f"生成代码过长，最大允许 {max_chars} 个字符")

    return ExtractedCode(code=code, warnings=tuple(warnings))


def validate_generated_code(code: str, *, max_chars: int) -> ast.Module:
    """解析并校验生成代码，返回 AST 供后续使用。"""
    if not code.strip():
        raise UnsafeCodeError("生成代码为空")
    if len(code) > max_chars:
        raise UnsafeCodeError(f"生成代码过长，最大允许 {max_chars} 个字符")

    try:
        tree = ast.parse(code, filename="<generated>", mode="exec")
    except SyntaxError as exc:
        line = f"（第 {exc.lineno} 行）" if exc.lineno else ""
        raise UnsafeCodeError(f"代码语法错误{line}: {exc.msg}") from exc

    if not _has_top_level_result_assignment(tree):
        raise UnsafeCodeError("代码必须在模块顶层给 result_df 赋值")

    for node in ast.walk(tree):
        if isinstance(node, _DENY_NODE_TYPES):
            raise UnsafeCodeError(f"禁止使用 {type(node).__name__}（第 {node.lineno} 行）")

        if isinstance(node, ast.Name):
            if node.id in _DENY_NAMES or _is_dunder(node.id):
                raise UnsafeCodeError(f"禁止使用名称 {node.id}（第 {node.lineno} 行）")

        if isinstance(node, ast.Attribute):
            if _is_dunder(node.attr) or node.attr in _DENY_ATTRS:
                raise UnsafeCodeError(f"禁止调用属性 {node.attr}（第 {node.lineno} 行）")

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _is_dunder(node.name):
                raise UnsafeCodeError(f"禁止定义双下划线名称 {node.name}（第 {node.lineno} 行）")

    return tree


def sanitize_error_text(value: object, *, limit: int = 500) -> str:
    """清理异常文本，避免把宿主机路径、控制字符或超长堆栈回传给模型。"""
    text = str(value or "").strip()
    text = re.sub(r"(?<![\w.-])/(?:[\w.@+-]+/)+[\w.@+-]+", "<path>", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _contains_result_assignment(code: str) -> bool:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return False
    return _has_top_level_result_assignment(tree)


def _has_top_level_result_assignment(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == _RESULT_NAME
                for target in node.targets
            ):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == _RESULT_NAME:
                return True
    return False


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")
