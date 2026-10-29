"""基于 HTTP API 的数据抓取器（第二阶段占位实现）。

保留该实现的目的：
- 强制上层只面向 DataFetcher 接口编程；
- 第二阶段切换数据源时仅需补全本类并用 DATA_SOURCE=api 启用；
- 当前调用会返回明确的 501，而不是静默使用错误数据。
"""

from __future__ import annotations

from app.adapters.base import DataFetcher, FetchRequest, FetchResult
from app.config import Settings
from app.core.exceptions import DataSourceNotConfiguredError


class APIFetcher(DataFetcher):
    """物业系统开放 API 适配器骨架。"""

    source = "api"

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.property_api_base_url
        self._api_key = settings.property_api_key

    async def fetch(self, request: FetchRequest) -> FetchResult:
        # TODO(phase-2):
        # 1. GET {base_url}/work-orders?start=&end=&project=
        # 2. 将 JSON 转为标准列 DataFrame
        # 3. 调用 normalize_work_order_frame 做统一兜底
        # 4. 返回 FetchResult(source="api", ...)
        raise DataSourceNotConfiguredError(
            "APIFetcher 尚未接入，请切换 DATA_SOURCE=rpa 或补全 API 数据源",
            details={"source": self.source, "configured": bool(self._base_url)},
        )

    async def healthcheck(self) -> bool:
        return False
