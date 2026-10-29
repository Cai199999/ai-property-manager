"""DataFetcher 抽象层。

设计要点：
1. 上层只依赖 DataFetcher，不依赖 playwright、requests 或任何具体数据源。
2. 所有实现都必须返回带标准列的 pandas DataFrame。
3. 新增数据源只需实现本接口，并在 registry 中注册，无需修改 Agent。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Mapping

import pandas as pd

from app.schemas.assistant import TimeRange


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """一次抓取请求。"""

    time_range: TimeRange
    property_name: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchResult:
    """数据源返回的统一结果。"""

    frame: pd.DataFrame
    source: str
    fetched_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class DataFetcher(ABC):
    """数据抓取器接口。

    实现类应保证：
    - fetch 可被并发调用；
    - 不在日志中输出账号、Cookie、密钥等敏感信息；
    - 返回的 DataFrame 使用 ``app.domain.work_orders`` 中的标准列名。
    """

    source: ClassVar[str]

    @abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchResult:
        """按时间范围抓取并返回工单明细。"""

    async def healthcheck(self) -> bool:
        """数据源健康检查。默认认为已配置的适配器可用。"""
        return True
