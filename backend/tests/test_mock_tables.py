import json
from pathlib import Path

import pandas as pd

from app.adapters.report_schema import REPORT_COLUMNS
from app.services.mock_tables import generate_mock_tables


def _read_raw(path: Path) -> pd.DataFrame:
    """模拟用户拿到的原始导出件：标题行 + 表头行 + 明细。"""
    return pd.read_excel(path, header=1)


def test_generate_mock_tables_creates_homogeneous_files(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path)

    names = {"工单表_A区", "工单表_B区", "工单表_C区"}
    assert set(summary["tables"]) == names
    assert summary["columns_per_table"] == 46
    assert summary["total_work_orders"] == sum(summary["tables"].values())

    frames = {}
    header_signature: tuple[str, ...] | None = None
    for name in names:
        path = tmp_path / f"{name}.xlsx"
        assert path.is_file()
        frame = _read_raw(path)
        frames[name] = frame

        # 原始文件必须与 report+(5).xlsx 一样是 46 列，且顺序完全一致。
        assert tuple(frame.columns) == tuple(REPORT_COLUMNS)
        assert len(frame) == summary["tables"][name]
        # 费用金额只在分析时派生，不能污染用户的原始导出件。
        assert "费用金额" not in frame.columns

        signature = tuple(frame.columns)
        if header_signature is None:
            header_signature = signature
        else:
            assert signature == header_signature

    # 区域前缀保证三个文件的单号全局唯一，concat 后不会重复计数。
    ids = pd.concat([frame["单号"] for frame in frames.values()], ignore_index=True)
    assert ids.is_unique
    assert len(ids) == summary["total_work_orders"]
    assert ids.str.startswith("A-").sum() == len(frames["工单表_A区"])
    assert ids.str.startswith("B-").sum() == len(frames["工单表_B区"])
    assert ids.str.startswith("C-").sum() == len(frames["工单表_C区"])

    # 旧的外键 Mock 表必须被清理。
    for legacy in ("工单表.xlsx", "项目表.xlsx", "人员表.xlsx"):
        assert not (tmp_path / legacy).exists()


def test_manifest_declares_homogeneous_group(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == 2
    group = manifest["schema_groups"][0]
    assert group["name"] == "work_orders"
    assert group["tables"] == ["工单表_A区", "工单表_B区", "工单表_C区"]
    assert group["columns"] == list(REPORT_COLUMNS)
    assert group["concat_recommended"] is True

    entries = {entry["name"]: entry for entry in manifest["tables"]}
    assert set(entries) == set(summary["tables"])
    for entry in entries.values():
        assert entry["role"] == "fact"
        assert entry["schema_group"] == "work_orders"
        assert entry["time_column"] == "登记时间"
        assert entry["primary_key"] == ["单号"]
        assert "foreign_keys" not in entry
