from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.services.mock_tables import generate_mock_tables

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "rpa_download_dir": tmp_path / "downloads",
        "data_table_dir": tmp_path / "tables",
        "table_directory_enabled": True,
        "table_upload_max_bytes": 1_000_000,
        "table_upload_max_uncompressed_bytes": 10_000_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _mock_files(tmp_path: Path) -> tuple[dict[str, int], Path]:
    source_dir = tmp_path / "source"
    summary = generate_mock_tables(source_dir)
    return summary["tables"], source_dir


def _xlsx_bytes(source_dir: Path, name: str) -> bytes:
    return (source_dir / f"{name}.xlsx").read_bytes()


def test_upload_adds_table_and_returns_catalog(tmp_path: Path) -> None:
    rows, source_dir = _mock_files(tmp_path)
    app = create_app(_settings(tmp_path))
    content = _xlsx_bytes(source_dir, "工单表_A区")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={
                "file": (
                    "园区工单表.xlsx",
                    content,
                    _XLSX_MEDIA_TYPE,
                )
            },
        )
        catalog = client.get("/api/v1/assistant/tables")

    assert response.status_code == 200
    body = response.json()
    assert body["imported"]["name"] == "园区工单表"
    assert body["imported"]["row_count"] == rows["工单表_A区"]
    # 上传后进入内存分析表，自动派生费用金额列。
    assert body["imported"]["column_count"] == 47
    assert body["primary_table"] == "园区工单表"
    assert any(table["name"] == "园区工单表" for table in body["tables"])
    assert catalog.status_code == 200
    assert any(table["name"] == "园区工单表" for table in catalog.json()["tables"])
    assert list((tmp_path / "tables").glob("upload_*.xlsx"))


def test_upload_duplicate_content_reuses_existing_table(tmp_path: Path) -> None:
    """内容完全一致时返回 200 + warning，不落盘第二份副本。"""
    _, source_dir = _mock_files(tmp_path)
    app = create_app(_settings(tmp_path))
    content = _xlsx_bytes(source_dir, "工单表_A区")

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/assistant/tables/upload",
            files={"file": ("园区工单表.xlsx", content, _XLSX_MEDIA_TYPE)},
        )
        second = client.post(
            "/api/v1/assistant/tables/upload",
            files={"file": ("重复副本.xlsx", content, _XLSX_MEDIA_TYPE)},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.json()
    assert body["imported"]["name"] == "园区工单表"
    assert any("完全一致" in warning and "园区工单表" in warning for warning in body["warnings"])
    assert len(list((tmp_path / "tables").glob("upload_*.xlsx"))) == 1
    assert len(body["tables"]) == 1
    index = (tmp_path / "tables" / "upload_index.json").read_text(encoding="utf-8")
    assert index.count("original_stem") == 1


def test_upload_accepts_multiple_files_in_one_request(tmp_path: Path) -> None:
    rows, source_dir = _mock_files(tmp_path)
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files=[
                (
                    "file",
                    (
                        "九月A区工单.xlsx",
                        _xlsx_bytes(source_dir, "工单表_A区"),
                        _XLSX_MEDIA_TYPE,
                    ),
                ),
                (
                    "file",
                    (
                        "九月B区工单.xlsx",
                        _xlsx_bytes(source_dir, "工单表_B区"),
                        _XLSX_MEDIA_TYPE,
                    ),
                ),
            ],
        )

    assert response.status_code == 200
    body = response.json()
    imported = {item["name"]: item for item in body["imported_tables"]}
    assert set(imported) == {"九月A区工单", "九月B区工单"}
    assert imported["九月A区工单"]["row_count"] == rows["工单表_A区"]
    assert imported["九月B区工单"]["row_count"] == rows["工单表_B区"]
    assert body["imported"]["name"] == "九月A区工单"
    assert len(list((tmp_path / "tables").glob("upload_*.xlsx"))) == 2


def test_upload_rejects_too_many_files(tmp_path: Path) -> None:
    _, source_dir = _mock_files(tmp_path)
    app = create_app(_settings(tmp_path, table_upload_max_files=1))
    content = _xlsx_bytes(source_dir, "工单表_A区")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files=[
                ("file", ("A.xlsx", content, _XLSX_MEDIA_TYPE)),
                ("file", ("B.xlsx", content, _XLSX_MEDIA_TYPE)),
            ],
        )

    assert response.status_code == 422
    assert "最多导入" in response.json()["error"]["message"]


def test_upload_uses_filename_form_field_over_part_name(tmp_path: Path) -> None:
    """网关用 ASCII 文件段名 + 独立 filename 字段透传中文原始名。"""
    _, source_dir = _mock_files(tmp_path)
    app = create_app(_settings(tmp_path))
    content = _xlsx_bytes(source_dir, "工单表_A区")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/tables/upload",
            files={
                "file": (
                    "upload_1.xlsx",
                    content,
                    _XLSX_MEDIA_TYPE,
                )
            },
            data={"filename": "园区工单表.xlsx"},
        )

    assert response.status_code == 200
    assert response.json()["imported"]["name"] == "园区工单表"


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
    app = create_app(_settings(tmp_path, table_upload_max_bytes=100_000))

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
