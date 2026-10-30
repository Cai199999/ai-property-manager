import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.core.exceptions import TableDeletionError
from app.main import create_app
from app.services.mock_tables import generate_mock_tables
from app.services.query_understanding import QueryUnderstanding
from app.services.table_catalog import TableCatalog
from app.services.table_deletion import DELETE_SECURITY_MESSAGE, TableDeletionService

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _FakeUnderstanding:
    """确定性意图替身：单元测试不依赖真实大模型。"""

    def __init__(
        self,
        intent: str,
        table_name: str | None = None,
        reason: str = "",
    ) -> None:
        self.intent = intent
        self.table_name = table_name
        self.reason = reason

    async def understand(
        self,
        message: str,
        *,
        table_names: list[str],
        provider: object | None = None,
    ) -> QueryUnderstanding:
        return QueryUnderstanding(
            intent=self.intent,  # type: ignore[arg-type]
            table_name=self.table_name,
            reason=self.reason,
        )

    async def aclose(self) -> None:
        return None


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


def _app(
    tmp_path: Path,
    understanding: _FakeUnderstanding,
) -> object:
    return create_app(
        _settings(tmp_path),
        extractor=_ExplodingExtractor(),  # type: ignore[arg-type]
        code_generator=_ExplodingGenerator(),  # type: ignore[arg-type]
        query_understanding=understanding,
    )


def test_delete_table_api_removes_file_manifest_and_bypasses_llm(
    tmp_path: Path,
) -> None:
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_specific_table", "工单表_A区"))

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

    app = _app(tmp_path, _FakeUnderstanding("delete_specific_table", "工单表_A区"))
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
    app = _app(tmp_path, _FakeUnderstanding("delete_specific_table", "不存在的表"))

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
    assert body["candidates"] == ["工单表_A区", "工单表_B区", "工单表_C区"]
    assert (tmp_path / "工单表_A区.xlsx").exists()


def test_ambiguous_delete_never_touches_files(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    app = _app(
        tmp_path,
        _FakeUnderstanding("ambiguous_delete", reason="删除目标不明确"),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除那张表"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "action_result"
    assert body["action"] == "none"
    assert body["success"] is False
    assert body["message"] == "删除目标不明确"
    assert len(list(tmp_path.glob("*.xlsx"))) == 3


def test_delete_all_requires_confirmation_then_clears_directory(
    tmp_path: Path,
) -> None:
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_all_tables"))

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/assistant/query/code",
            headers={"X-Request-Id": "delete-all-preview"},
            json={"message": "删除全部表"},
        )
        assert first.status_code == 200
        preview = first.json()
        assert preview["type"] == "confirmation_required"
        assert preview["action"] == "delete_all_tables"
        assert preview["message"] == "检测到您要删除所有数据表，此操作不可逆，是否确认？"
        assert preview["table_count"] == 3
        assert preview["confirmation_token"]
        assert preview["request_id"] == "delete-all-preview"

        # 确认前绝不删除任何文件。
        assert len(list(tmp_path.glob("*.xlsx"))) == 3

        confirm = client.post(
            "/api/v1/assistant/tables/delete-all/confirm",
            headers={"X-Request-Id": "delete-all-confirm"},
            json={"confirmation_token": preview["confirmation_token"]},
        )

    assert confirm.status_code == 200
    body = confirm.json()
    assert body["type"] == "action_result"
    assert body["action"] == "delete_all_tables"
    assert body["success"] is True
    assert body["message"] == "已成功删除全部数据表（3 张）"
    assert body["remaining_tables"] == []
    assert set(body["deleted_tables"]) == {"工单表_A区", "工单表_B区", "工单表_C区"}

    assert not list(tmp_path.glob("*.xlsx"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"] == []
    assert manifest["schema_groups"] == []
    index_path = tmp_path / "upload_index.json"
    if index_path.exists():
        assert json.loads(index_path.read_text(encoding="utf-8")) == {}


def test_delete_all_cancel_keeps_every_table(tmp_path: Path) -> None:
    """取消确认等价于不调用确认接口，令牌自然过期。"""
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_all_tables"))

    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "清空所有数据表"},
        )
        assert preview.status_code == 200
        assert preview.json()["type"] == "confirmation_required"

    assert len(list(tmp_path.glob("*.xlsx"))) == 3


def test_delete_all_token_is_single_use(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_all_tables"))

    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除全部表"},
        ).json()
        token = preview["confirmation_token"]

        first = client.post(
            "/api/v1/assistant/tables/delete-all/confirm",
            json={"confirmation_token": token},
        )
        second = client.post(
            "/api/v1/assistant/tables/delete-all/confirm",
            json={"confirmation_token": token},
        )

    assert first.json()["success"] is True
    assert second.status_code == 200
    assert second.json()["success"] is False
    assert "已使用" in second.json()["message"]


def test_delete_all_rejects_fingerprint_change(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_all_tables"))

    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除全部表"},
        ).json()

        # 模拟确认期间其他操作删掉了一张表，令牌必须失效。
        (tmp_path / "工单表_A区.xlsx").unlink()

        confirm = client.post(
            "/api/v1/assistant/tables/delete-all/confirm",
            json={"confirmation_token": preview["confirmation_token"]},
        )

    assert confirm.status_code == 200
    body = confirm.json()
    assert body["success"] is False
    assert "已发生变化" in body["message"]
    assert not (tmp_path / "工单表_A区.xlsx").exists()
    assert (tmp_path / "工单表_B区.xlsx").exists()
    assert (tmp_path / "工单表_C区.xlsx").exists()


def test_delete_all_without_tables_returns_failure(tmp_path: Path) -> None:
    app = _app(tmp_path, _FakeUnderstanding("delete_all_tables"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query/code",
            json={"message": "删除全部表"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "action_result"
    assert body["action"] == "delete_all_tables"
    assert body["success"] is False
    assert "没有可删除" in body["message"]


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
    ("message", "table_name"),
    [
        ("删除../工单表_A区.xlsx", "../工单表_A区.xlsx"),
        ("删除 ../工单表_A区", "../工单表_A区"),
        ("删除 /etc/工单表_A区", "/etc/工单表_A区"),
        ("移除 ~/工单表_A区", "~/工单表_A区"),
    ],
)
def test_delete_intent_rejects_path_like_input(
    tmp_path: Path,
    message: str,
    table_name: str,
) -> None:
    """自然语言入口同样不能借路径穿越绕过表名白名单。"""
    generate_mock_tables(tmp_path)
    app = _app(tmp_path, _FakeUnderstanding("delete_specific_table", table_name))

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


def test_delete_all_skips_external_symlink(tmp_path: Path) -> None:
    """批量删除同样只能删除工作区内真实文件。"""
    tables_dir = tmp_path / "tables"
    generate_mock_tables(tables_dir)
    outside = tmp_path / "outside-source.xlsx"
    outside.write_bytes((tables_dir / "工单表_A区.xlsx").read_bytes())
    link = tables_dir / "upload_evil.xlsx"
    link.symlink_to(outside)

    settings = _settings(tables_dir)
    service = TableDeletionService(settings, TableCatalog(settings))
    files, fingerprint = service.table_set_fingerprint()
    result = service.delete_all_tables(expected_fingerprint=fingerprint)

    assert set(result.deleted_tables) == {"工单表_A区", "工单表_B区", "工单表_C区"}
    assert files == ("工单表_A区.xlsx", "工单表_B区.xlsx", "工单表_C区.xlsx")
    for name in files:
        assert not (tables_dir / name).exists()
    # 越界软链接本身不是受管副本，保留它也不会再被扫描为数据表。
    assert link.exists()
    assert outside.exists()


def test_delete_all_rejects_stale_fingerprint_at_service_layer(tmp_path: Path) -> None:
    tables_dir = tmp_path / "tables"
    generate_mock_tables(tables_dir)
    settings = _settings(tables_dir)
    service = TableDeletionService(settings, TableCatalog(settings))

    with pytest.raises(TableDeletionError) as error:
        service.delete_all_tables(expected_fingerprint="stale")

    assert "已发生变化" in error.value.message
    assert (tables_dir / "工单表_A区.xlsx").exists()
