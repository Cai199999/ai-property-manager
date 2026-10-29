"""时间范围解析的公共模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.schemas.assistant import TimeRange


@dataclass(frozen=True, slots=True)
class TimeExtraction:
    """时间语义解析结果。"""

    time_range: TimeRange
    engine: Literal["deepseek", "rule_based"]
    model: str | None
    confidence: float
    reason: str
    warnings: tuple[str, ...] = ()
