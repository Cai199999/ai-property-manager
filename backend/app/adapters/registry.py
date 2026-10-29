"""数据源适配器注册表。"""

from __future__ import annotations

from collections.abc import Callable

from app.adapters.api_fetcher import APIFetcher
from app.adapters.base import DataFetcher
from app.adapters.rpa_fetcher import RPAFetcher
from app.config import Settings
from app.core.exceptions import DataSourceNotConfiguredError

FetcherBuilder = Callable[[Settings], DataFetcher]


class DataFetcherRegistry:
    """按配置名创建 DataFetcher。

    通过注册表新增数据源，不需要修改 Agent 或 API 路由：
        registry.register("mcp", lambda settings: McpFetcher(settings))
    """

    def __init__(self) -> None:
        self._builders: dict[str, FetcherBuilder] = {}

    def register(self, source: str, builder: FetcherBuilder) -> None:
        normalized = source.strip().lower()
        if not normalized:
            raise ValueError("数据源名称不能为空")
        self._builders[normalized] = builder

    def create(self, source: str, settings: Settings) -> DataFetcher:
        builder = self._builders.get(source.strip().lower())
        if builder is None:
            raise DataSourceNotConfiguredError(
                f"未注册的数据源: {source}",
                details={"available_sources": sorted(self._builders)},
            )
        return builder(settings)

    @property
    def available_sources(self) -> list[str]:
        return sorted(self._builders)


def create_default_registry() -> DataFetcherRegistry:
    """注册第一阶段支持的两种数据源。"""
    registry = DataFetcherRegistry()
    registry.register(
        "rpa",
        lambda settings: RPAFetcher(
            download_dir=settings.rpa_download_dir,
            simulate_delay_seconds=settings.rpa_simulate_delay_seconds,
        ),
    )
    registry.register("api", lambda settings: APIFetcher(settings))
    return registry
