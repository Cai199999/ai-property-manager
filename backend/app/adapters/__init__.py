"""数据源适配器。"""

from app.adapters.base import DataFetcher, FetchRequest, FetchResult
from app.adapters.registry import DataFetcherRegistry, create_default_registry

__all__ = [
    "DataFetcher",
    "DataFetcherRegistry",
    "FetchRequest",
    "FetchResult",
    "create_default_registry",
]
