from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.services.mock_tables import generate_mock_tables


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "rpa_download_dir": tmp_path / "downloads",
        "data_table_dir": tmp_path / "tables",
        "table_directory_enabled": True,
        "table_upload_max_bytes": 100_000,
        "table_upload_max_uncompressed_bytes": 10_000_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _xlsx_bytes(tmp_path: Path) -> bytes:
    source_dir = tmp_path / "source"
    generate_mock_tables(source_dir)
    return (source_dir / "项目表.xlsx").read_bytes()


def test_upload_adds_table_and_returns_catalog(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    content = _xlsx_bytes(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={
                "file": (
                    "园区项目表.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        catalog = client.get("/api/v1/assistant/tables")

    assert response.status_code == 200
    body = response.json()
    assert body["imported"]["name"] == "园区项目表"
    assert body["imported"]["row_count"] == 8
    assert body["imported"]["column_count"] == 9
    assert body["primary_table"] == "园区项目表"
    assert any(table["name"] == "园区项目表" for table in body["tables"])
    assert catalog.status_code == 200
    assert any(table["name"] == "园区项目表" for table in catalog.json()["tables"])
    assert list((tmp_path / "tables").glob("upload_*.xlsx"))


def test_upload_uses_filename_form_field_over_part_name(tmp_path: Path) -> None:
    """网关用 ASCII 文件段名 + 独立 filename 字段透传中文原始名。"""
    app = create_app(_settings(tmp_path))
    content = _xlsx_bytes(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={
                "file": (
                    "upload.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"filename": "园区项目表.xlsx"},
        )

    assert response.status_code == 200
    assert response.json()["imported"]["name"] == "园区项目表"


def test_upload_rejects_non_xlsx(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={"file": ("恶意.txt", b"hello", "text/plain")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TABLE_IMPORT_FAILED"


def test_upload_rejects_oversized_file(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={"file": ("big.xlsx", b"x" * 100_001, "application/octet-stream")},
        )

    assert response.status_code == 422
    assert "MB" in response.json()["error"]["message"]


def test_upload_endpoint_returns_503_when_directory_disabled(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, table_directory_enabled=False))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={"file": ("test.xlsx", b"abc", "application/octet-stream")},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TABLE_IMPORT_UNAVAILABLE"
