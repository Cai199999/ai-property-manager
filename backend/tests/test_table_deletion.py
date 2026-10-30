import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import TableDeletionError
from app.main import create_app
from app.services.mock_tables import generate_mock_tables
from app.services.table_catalog import TableCatalog
from app.services.table_delete_intent import (
    DeleteTableIntent,
    DeleteTableIssue,
    detect_delete_intent,
)
from app.services.table_deletion import DELETE_SECURITY_MESSAGE, TableDeletionService

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _ExplodingExtractor:
    async def extract(self, message: str, *, provider: object | None = None) -> object:
        raise AssertionError("删表请求不应调用时间解析模型")

    async def aclose(self) -> None:
        return None


class _ExplodingGenerator:
    async def complete(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("删表请求不应调用代码生成模型")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "data_table_dir": tmp_path,
        "table_directory_enabled": True,
        "report_export_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_detect_delete_intent_examples() -> None:
    names = ["工单表_A区", "工单表_B区", "工单表_C区"]

    first = detect_delete_intent("帮我把 A区的表删掉", names)
    assert isinstance(first, DeleteTableIntent)
    assert first.table_name == "工单表_A区"

    second = detect_delete_intent("移除工单表_B区", names)
    assert isinstance(second, DeleteTableIntent)
    assert second.table_name == "工单表_B区"

    third = detect_delete_intent("删除工单表_A区", names)
    assert isinstance(third, DeleteTableIntent)
    assert third.table_name == "工单表_A区"


def test_detect_delete_intent_ignores_data_content_deletion() -> None:
    names = ["工单表_A区", "工单表_B区"]

    assert detect_delete_intent("删除工单表_A区里的数据", names) is None
    assert detect_delete_intent("删除A区上个月的数据", names) is None
    assert detect_delete_intent("查看工单表_A区", names) is None


def test_detect_delete_intent_reports_missing_or_ambiguous_tables() -> None:
    missing = detect_delete_intent("删除不存在的表", ["工单表_A区"])
    assert isinstance(missing, DeleteTableIssue)
    assert "未找到数据表" in missing.message

    ambiguous = detect_delete_intent(
        "删除A区工单表",
        ["工单表_A区", "A区工单"],
    )
    assert isinstance(ambiguous, DeleteTableIssue)
    assert set(ambiguous.candidates) == {"工单表_A区", "A区工单"}


def test_delete_table_api_removes_file_manifest_and_bypasses_llm(
    tmp_path: Path,
) -> None:
    generate_mock_tables(tmp_path)
    app = create_app(
        _settings(tmp_path),
        extractor=_ExplodingExtractor(),  # type: ignore[arg-type]
        code_generator=_ExplodingGenerator(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除工单表_A区"},
        )
        catalog = client.get("/api/v1/assistant/tables")

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "action_result"
    assert body["action"] == "delete_table"
    assert body["success"] is True
    assert body["message"] == "已成功删除数据表：工单表_A区"
    assert body["deleted_table"] == "工单表_A区"
    assert body["remaining_tables"] == ["工单表_B区", "工单表_C区"]

    assert not (tmp_path / "工单表_A区.xlsx").exists()
    assert (tmp_path / "工单表_B区.xlsx").exists()
    assert (tmp_path / "工单表_C区.xlsx").exists()

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert all(entry["name"] != "工单表_A区" for entry in manifest["tables"])
    assert all("工单表_A区" not in entry.get("concat_tables", []) for entry in manifest["tables"])
    for group in manifest["schema_groups"]:
        assert "工单表_A区" not in group.get("tables", [])

    assert catalog.status_code == 200
    assert [table["name"] for table in catalog.json()["tables"]] == [
        "工单表_B区",
        "工单表_C区",
    ]


def test_delete_table_api_removes_upload_index_and_duplicate_copies(
    tmp_path: Path,
) -> None:
    generate_mock_tables(tmp_path)
    original = tmp_path / "工单表_A区.xlsx"
    duplicate = tmp_path / "upload_deadbeef_工单表_A区.xlsx"
    duplicate.write_bytes(original.read_bytes())
    index_path = tmp_path / "upload_index.json"
    index_path.write_text(
        json.dumps(
            {
                duplicate.name: {
                    "original_stem": "工单表_A区",
                    "sheet_name": "Sheet0",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除工单表_A区"},
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert not original.exists()
    assert not duplicate.exists()
    assert json.loads(index_path.read_text(encoding="utf-8")) == {}


def test_delete_unknown_table_returns_failed_action_result(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除不存在的表"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "action_result"
    assert body["success"] is False
    assert "未找到数据表" in body["message"]
    assert (tmp_path / "工单表_A区.xlsx").exists()


@pytest.mark.parametrize(
    "table_name",
    [
        "../secret.xlsx",
        "../../etc/passwd",
        "/etc/passwd",
        "~/secret.xlsx",
        "tables\\..\\secret.xlsx",
        "..",
        "./工单表_A区.xlsx",
    ],
)
def test_delete_table_tool_rejects_path_traversal(
    tmp_path: Path,
    table_name: str,
) -> None:
    """任何路径形态的表名都必须在查表/落盘之前被拒绝。"""
    tables_dir = tmp_path / "tables"
    generate_mock_tables(tables_dir)
    secret = tmp_path / "secret.xlsx"
    secret.write_bytes((tables_dir / "工单表_A区.xlsx").read_bytes())
    settings = _settings(tables_dir)
    service = TableDeletionService(settings, TableCatalog(settings))

    with pytest.raises(TableDeletionError) as error:
        service.delete_table_tool(table_name)

    assert error.value.message == DELETE_SECURITY_MESSAGE
    assert (tables_dir / "工单表_A区.xlsx").exists()
    assert secret.exists()


def test_delete_table_tool_rejects_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    tables_dir = tmp_path / "tables"
    generate_mock_tables(tables_dir)
    outside = tmp_path / "outside-source.xlsx"
    outside.write_bytes((tables_dir / "工单表_A区.xlsx").read_bytes())
    settings = _settings(tables_dir)
    service = TableDeletionService(settings, TableCatalog(settings))

    with pytest.raises(TableDeletionError) as error:
        service.delete_table_tool(str(outside))

    assert error.value.message == DELETE_SECURITY_MESSAGE
    assert outside.exists()


@pytest.mark.parametrize(
    "message",
    [
        "删除../工单表_A区.xlsx",
        "删除 ../工单表_A区",
        "删除 /etc/工单表_A区",
        "移除 ~/工单表_A区",
    ],
)
def test_delete_intent_rejects_path_like_input(
    tmp_path: Path,
    message: str,
) -> None:
    """自然语言入口同样不能借路径穿越绕过表名白名单。"""
    generate_mock_tables(tmp_path)
    app = create_app(
        _settings(tmp_path),
        extractor=_ExplodingExtractor(),  # type: ignore[arg-type]
        code_generator=_ExplodingGenerator(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": message},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "action_result"
    assert body["success"] is False
    assert body["message"] == DELETE_SECURITY_MESSAGE
    assert (tmp_path / "工单表_A区.xlsx").exists()


def test_delete_does_not_follow_workspace_symlink_to_outside(
    tmp_path: Path,
) -> None:
    """工作区内指向外部的软链接即使内容相同，也绝不能删除外部目标。"""
    tables_dir = tmp_path / "tables"
    generate_mock_tables(tables_dir)
    outside = tmp_path / "outside-source.xlsx"
    outside.write_bytes((tables_dir / "工单表_A区.xlsx").read_bytes())
    link = tables_dir / "upload_evil.xlsx"
    link.symlink_to(outside)

    settings = _settings(tables_dir)
    service = TableDeletionService(settings, TableCatalog(settings))
    result = service.delete_table_tool("工单表_A区")

    assert result.deleted_table == "工单表_A区"
    assert not (tables_dir / "工单表_A区.xlsx").exists()
    assert outside.exists()
    assert link.exists()
