import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.services.mock_tables import generate_mock_tables
from app.services.table_catalog import TableCatalog
from app.services.table_preview import build_table_preview


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "rpa_download_dir": tmp_path / "downloads",
        "data_table_dir": tmp_path / "tables",
        "table_directory_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_preview_returns_raw_excel_columns_only(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path / "tables")
    snapshot = TableCatalog(_settings(tmp_path)).scan()
    dataset = snapshot.table_map()["工单表_A区"]

    preview = build_table_preview(dataset, offset=0, limit=5)

    assert preview["name"] == "工单表_A区"
    assert preview["total_rows"] == summary["tables"]["工单表_A区"]
    assert preview["offset"] == 0
    assert preview["limit"] == 5
    assert preview["has_more"] is True
    # 只展示磁盘文件中的 46 列，派生费用金额单独声明。
    assert len(preview["columns"]) == 46
    assert "费用金额" not in {column["name"] for column in preview["columns"]}
    assert preview["derived_columns"] == ["费用金额"]
    assert len(preview["rows"]) == 5
    # 时间字段必须转成字符串，NaN/NaT 不能进入 JSON。
    json.dumps(preview["rows"], ensure_ascii=False)


def test_preview_pages_to_the_end(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path / "tables")
    snapshot = TableCatalog(_settings(tmp_path)).scan()
    dataset = snapshot.table_map()["工单表_A区"]

    preview = build_table_preview(dataset, offset=418, limit=50)

    assert len(preview["rows"]) == summary["tables"]["工单表_A区"] - 418
    assert preview["has_more"] is False


def test_preview_api_returns_paginated_rows(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path / "tables")
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/assistant/tables/preview",
            params={"name": "工单表_A区", "offset": 10, "limit": 20},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "工单表_A区"
    assert body["offset"] == 10
    assert body["limit"] == 20
    assert len(body["rows"]) == 20
    assert len(body["columns"]) == 46
    assert body["derived_columns"] == ["费用金额"]


def test_preview_api_rejects_unknown_table(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path / "tables")
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/assistant/tables/preview",
            params={"name": "不存在的表"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TABLE_NOT_FOUND"


def test_preview_api_rejects_invalid_paging(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path / "tables")
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/assistant/tables/preview",
            params={"name": "工单表_A区", "limit": 201},
        )

    assert response.status_code == 422


def test_preview_api_unavailable_without_directory(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, table_directory_enabled=False))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/assistant/tables/preview",
            params={"name": "工单表_A区"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TABLE_PREVIEW_UNAVAILABLE"
