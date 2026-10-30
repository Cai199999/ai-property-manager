"""时间范围解析的公共模型。"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.assistant import ParserEngine, TimeRange


@dataclass(frozen=True, slots=True)
class TimeExtraction:
    """时间语义解析结果。"""

    time_range: TimeRange
    engine: ParserEngine
    model: str | None
    confidence: float
    reason: str
    warnings: tuple[str, ...] = ()
