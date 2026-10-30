"""规则化的“删除数据表”意图识别。

只处理结构性删除（删除整张表 / Excel 文件），不处理删除行、删除某段时间
数据等数据内容操作。规则故意保守：无法唯一确定表名时返回待确认结果，
绝不猜测后直接删除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# 常见删除动词；不使用单独的“清空”，避免把清空内容误判为删除表。
_ACTION_RE = re.compile(r"(?:删除|删掉|删去|删了|移除|去掉|清除|干掉|清理掉)")
# 结构性对象提示词，确保“删除 A 区数据”不会被当成删表。
_TABLE_HINT_RE = re.compile(r"(?:数据表|工作表|表格|表|\.xlsx|excel|文件)", re.IGNORECASE)
# 明确指向表内数据/记录，属于数据清洗而不是删表。
_ROW_SCOPE_RE = re.compile(
    r"(?:里|中|内)的?(?:数据|记录|行|条目|内容)" r"|(?:数据行|记录行|全部数据|所有数据)"
)
_STRUCTURAL_HINT_RE = re.compile(
    r"(?:数据表|工作表|表格|整张表|整表|这张表|该表|这个表|\.xlsx)",
    re.IGNORECASE,
)
# 提取用户口述的表名时，去掉这些语气词和结构性后缀。
_LEADING_NOISE_RE = re.compile(
    r"^(?:请|麻烦|帮我|帮忙|给我|把|将|需要|想要|我要|一下|这个|那个|名为|叫做|\s)+"
)
_TRAILING_NOISE_RE = re.compile(
    r"(?:这个表|那张表|这张表|数据表|工作表|表格|表|文件|吧|啊|呀|谢谢|一下|"
    r"删掉|删除|移除|去掉|\s|[。.!！?？,，;；])+$"
)
_PUNCTUATION_RE = re.compile(r"[「」『』“”\"'《》\[\]（）()\s]+")


@dataclass(frozen=True, slots=True)
class DeleteTableIntent:
    """已唯一确定目标表的删除意图。"""

    table_name: str
    requested_name: str


@dataclass(frozen=True, slots=True)
class DeleteTableIssue:
    """确认是删表意图，但目标缺失或不唯一。"""

    message: str
    candidates: tuple[str, ...] = ()


def looks_like_table_delete(message: str) -> bool:
    """廉价预判是否需要读取表目录来识别删表意图。

    与 ``detect_delete_intent`` 使用同一组规则，避免普通查询多扫描一次目录。
    """
    raw = (message or "").strip()
    if not raw or _ACTION_RE.search(raw) is None:
        return False
    if _ROW_SCOPE_RE.search(raw):
        return False
    if not _TABLE_HINT_RE.search(raw):
        # “删除 A 区数据”这类请求不包含结构对象，交回普通查询流程。
        return False
    if re.search(r"(?:数据|记录|行|条目|内容)", raw) and not _STRUCTURAL_HINT_RE.search(raw):
        # 只有“表”字来自表名本身，且用户在说数据内容时，不删表。
        return False
    return True


def detect_delete_intent(
    message: str,
    table_names: Iterable[str],
) -> DeleteTableIntent | DeleteTableIssue | None:
    """识别删表意图。

    返回 ``None`` 表示不是删表请求，应继续走原有 Pandas 查询流程；
    返回 ``DeleteTableIssue`` 时由调用方直接提示用户，不调用大模型。
    """
    raw = (message or "").strip()
    if not looks_like_table_delete(raw):
        return None

    names = tuple(dict.fromkeys(name for name in table_names if name))
    normalized_message = _normalize(raw)
    matched = [
        name
        for name in names
        if any(alias and alias in normalized_message for alias in _aliases(name))
    ]
    if len(matched) > 1:
        return DeleteTableIssue(
            message=f"检测到多张可能的数据表：{'、'.join(matched)}，请指定完整表名",
            candidates=tuple(matched),
        )
    if len(matched) == 1:
        return DeleteTableIntent(table_name=matched[0], requested_name=matched[0])

    requested = _extract_requested_name(raw)
    if requested:
        return DeleteTableIssue(message=f"未找到数据表：{requested}")
    return DeleteTableIssue(message="请指定要删除的数据表名称")


def _aliases(name: str) -> tuple[str, ...]:
    """生成表名别名，兼容下划线/空格/前缀差异。

    例：``工单表_A区`` → ``工单表a区``、``a区``。
    """
    normalized = _normalize(name)
    aliases = {normalized}
    for prefix in ("工单表", "数据表", "工作表"):
        if normalized.startswith(_normalize(prefix)):
            remainder = normalized[len(_normalize(prefix)) :]
            if len(remainder) >= 2:
                aliases.add(remainder)
    return tuple(alias for alias in aliases if len(alias) >= 2)


def _normalize(value: str) -> str:
    normalized = _PUNCTUATION_RE.sub("", value).lower()
    normalized = normalized.removesuffix(".xlsx")
    return re.sub(r"[\s_\-—·]+", "", normalized)


def _extract_requested_name(message: str) -> str:
    """从“删除 XXX 表”中提取用户口述的表名，仅用于错误提示。"""
    match = _ACTION_RE.search(message)
    if match is None:
        return ""
    remainder = message[match.end() :]
    remainder = _LEADING_NOISE_RE.sub("", remainder)
    remainder = _TRAILING_NOISE_RE.sub("", remainder)
    remainder = remainder.strip("「」『』“”\"'《》[]（）() ")
    return remainder[:60]
