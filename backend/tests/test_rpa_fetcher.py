import asyncio
from datetime import date
from pathlib import Path

import pandas as pd

from app.adapters.base import FetchRequest
from app.adapters.rpa_fetcher import RPAFetcher
from app.domain.work_orders import REQUIRED_WORK_ORDER_COLUMNS, WorkOrderColumn
from app.schemas.assistant import TimeRange


def test_rpa_fetcher_downloads_excel_and_normalizes(tmp_path: Path) -> None:
    fetcher = RPAFetcher(tmp_path, simulate_delay_seconds=0)
    request = FetchRequest(
        time_range=TimeRange(start_date=date(2026, 9, 1), end_date=date(2026, 9, 5)),
        property_name="樱花苑",
    )

    result = asyncio.run(fetcher.fetch(request))

    assert result.source == "rpa"
    assert not result.frame.empty
    assert set(REQUIRED_WORK_ORDER_COLUMNS).issubset(result.frame.columns)
    assert result.frame[WorkOrderColumn.PROJECT.value].eq("樱花苑").all()

    received = pd.to_datetime(result.frame[WorkOrderColumn.RECEIVED_AT.value])
    assert received.min().date() >= date(2026, 9, 1)
    assert received.max().date() <= date(2026, 9, 5)

    exported = list(tmp_path.glob("*.xlsx"))
    assert len(exported) == 1
    assert result.metadata["source_file"] == exported[0].name


def test_rpa_fetcher_passes_project_filter_to_project_column(tmp_path: Path) -> None:
    fetcher = RPAFetcher(tmp_path, simulate_delay_seconds=0)
    request = FetchRequest(
        time_range=TimeRange(start_date=date(2026, 9, 1), end_date=date(2026, 9, 2)),
        property_name="翠竹园",
    )

    result = asyncio.run(fetcher.fetch(request))

    assert result.frame[WorkOrderColumn.PROJECT.value].unique().tolist() == ["翠竹园"]
