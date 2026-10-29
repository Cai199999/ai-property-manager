import json
from pathlib import Path

import pandas as pd

from app.services.mock_tables import generate_mock_tables


def test_generate_mock_tables_keeps_referential_integrity(tmp_path: Path) -> None:
    summary = generate_mock_tables(tmp_path)

    assert summary["work_orders"] == 150
    assert summary["staff"] == 24
    assert summary["projects"] == 8

    projects = pd.read_excel(tmp_path / "项目表.xlsx", header=1)
    staff = pd.read_excel(tmp_path / "人员表.xlsx", header=1)
    orders = pd.read_excel(tmp_path / "工单表.xlsx", header=1)

    assert {"项目编号", "项目名称", "所属区域", "物业经理"}.issubset(projects.columns)
    assert {"人员编号", "姓名", "所属项目编号"}.issubset(staff.columns)
    assert {"单号", "项目编号", "接单人编号", "维修人员编号"}.issubset(orders.columns)
    assert len(orders) == 150
    assert set(orders["项目编号"]) == set(projects["项目编号"])
    assert set(orders["接单人编号"]).issubset(set(staff["人员编号"]))
    assert set(orders["维修人员编号"]).issubset(set(staff["人员编号"]))

    # 每个项目至少有 3 单，避免低频项目在随机抽样中消失。
    assert orders["项目编号"].value_counts().min() >= 3

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in manifest["tables"]}
    assert names == {"工单表", "项目表", "人员表"}
    order_entry = next(
        entry for entry in manifest["tables"] if entry["name"] == "工单表"
    )
    assert order_entry["role"] == "fact"
    assert order_entry["time_column"] == "登记时间"
    assert len(order_entry["foreign_keys"]) == 3
