import json
from pathlib import Path

from app.config import Settings
from app.services.mock_tables import generate_mock_tables
from app.services.table_catalog import TableCatalog


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_table_dir=tmp_path,
        table_directory_enabled=True,
        table_max_files=8,
        table_max_rows=10_000,
        table_max_columns=100,
    )


def test_catalog_scans_tables_and_manifest_relationships(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    catalog = TableCatalog(_settings(tmp_path))

    snapshot = catalog.scan()

    assert snapshot.has_tables
    assert snapshot.primary_table == "工单表"
    assert {table.name for table in snapshot.tables} == {"工单表", "项目表", "人员表"}
    assert snapshot.warnings == ()

    orders = snapshot.table_map()["工单表"]
    assert orders.role == "fact"
    assert orders.row_count == 150
    assert orders.time_column == "登记时间"
    assert orders.primary_key == ("单号",)
    assert {foreign_key.referenced_table for foreign_key in orders.foreign_keys} == {
        "项目表",
        "人员表",
    }
    assert any(
        relationship["source"] == "manifest"
        and relationship["from_table"] == "工单表"
        and relationship["to_table"] == "项目表"
        for relationship in snapshot.relationships
    )

    # 同名列只作为候选提示，不能冒充 manifest 外键。
    assert any(
        hint["columns"] == ["项目名称", "项目编号"]
        for hint in orders.possible_join_keys
    )


def test_catalog_uses_upload_index_for_display_name(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    source = tmp_path / "项目表.xlsx"
    target = tmp_path / "upload_abc123_项目表.xlsx"
    target.write_bytes(source.read_bytes())
    (tmp_path / "upload_index.json").write_text(
        json.dumps({"upload_abc123_项目表.xlsx": {"original_stem": "园区项目表"}}),
        encoding="utf-8",
    )

    snapshot = TableCatalog(_settings(tmp_path)).scan()

    assert "园区项目表" in snapshot.table_map()
    uploaded = snapshot.table_map()["园区项目表"]
    assert uploaded.row_count == 8
    # 没有 manifest 声明的上传件不会被自动赋予外键。
    assert uploaded.foreign_keys == ()


def test_catalog_skips_corrupt_file_without_failing(tmp_path: Path) -> None:
    generate_mock_tables(tmp_path)
    (tmp_path / "损坏表.xlsx").write_bytes(b"not an excel file")

    snapshot = TableCatalog(_settings(tmp_path)).scan()

    assert snapshot.has_tables
    assert any("损坏表.xlsx" in warning for warning in snapshot.warnings)
