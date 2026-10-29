"""生成多表联查用的确定性 Mock Excel。

三张表的关系在生成阶段就固定下来：
- 项目表是主数据，项目编号 P001~P008；
- 人员表通过「所属项目编号」挂到项目表；
- 工单表通过「项目编号」「接单人编号」「维修人员编号」挂到前两张表，
  且接单/维修人员必须属于工单所在项目。

因此这里不是“随机生成后再祈祷能 join 上”，而是先构造主数据，再从主数据
取键填充事实表，最后用断言校验 100% 外键覆盖。
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.adapters.report_schema import (
    REPORT_COLUMN_WIDTHS,
    REPORT_COLUMNS,
    REPORT_SHEET_NAME,
)
from app.adapters.rpa_driver import SimulatedRpaExportDriver

MOCK_SEED = 20261029
WORK_ORDER_ROWS = 150

# 与现有 RPA 模拟器、前端下拉框保持一致的项目主数据。
PROJECTS: tuple[dict[str, Any], ...] = (
    {
        "项目编号": "P001",
        "项目名称": "望京国际研发园",
        "城市": "北京市",
        "所属区域": "朝阳区",
        "项目地址": "朝阳区望京街10号",
        "建筑面积": 128000,
        "交付年份": 2015,
        "业态类型": "产业园",
    },
    {
        "项目编号": "P002",
        "项目名称": "望京生命科学园",
        "城市": "北京市",
        "所属区域": "朝阳区",
        "项目地址": "朝阳区容和路1号",
        "建筑面积": 96000,
        "交付年份": 2018,
        "业态类型": "产业园",
    },
    {
        "项目编号": "P003",
        "项目名称": "望京科技园",
        "城市": "北京市",
        "所属区域": "朝阳区",
        "项目地址": "朝阳区利泽中园106号",
        "建筑面积": 72000,
        "交付年份": 2012,
        "业态类型": "写字楼",
    },
    {
        "项目编号": "P004",
        "项目名称": "望京数字创意园",
        "城市": "北京市",
        "所属区域": "朝阳区",
        "项目地址": "朝阳区酒仙桥北路9号",
        "建筑面积": 64000,
        "交付年份": 2019,
        "业态类型": "产业园",
    },
    {
        "项目编号": "P005",
        "项目名称": "望京未来信息园",
        "城市": "北京市",
        "所属区域": "海淀区",
        "项目地址": "海淀区中关村东路66号",
        "建筑面积": 58000,
        "交付年份": 2020,
        "业态类型": "写字楼",
    },
    {
        "项目编号": "P006",
        "项目名称": "慧谷时空",
        "城市": "北京市",
        "所属区域": "海淀区",
        "项目地址": "海淀区上地三街9号",
        "建筑面积": 41000,
        "交付年份": 2010,
        "业态类型": "写字楼",
    },
    {
        "项目编号": "P007",
        "项目名称": "金色家园",
        "城市": "北京市",
        "所属区域": "东城区",
        "项目地址": "东城区安定门外大街2号",
        "建筑面积": 36000,
        "交付年份": 2008,
        "业态类型": "住宅",
    },
    {
        "项目编号": "P008",
        "项目名称": "东湖街道办事处",
        "城市": "北京市",
        "所属区域": "东城区",
        "项目地址": "东城区东直门南大街5号",
        "建筑面积": 22000,
        "交付年份": 2006,
        "业态类型": "公建",
    },
)

_STAFF_NAMES: tuple[str, ...] = (
    "张伟",
    "王芳",
    "李强",
    "刘洋",
    "陈晨",
    "杨帆",
    "赵敏",
    "周磊",
    "吴静",
    "徐涛",
    "孙悦",
    "马超",
    "朱琳",
    "胡军",
    "郭婷",
    "何平",
    "高远",
    "林雪",
    "罗宾",
    "郑洁",
    "韩雪",
    "曹阳",
    "谢楠",
    "邓超",
)

_STAFF_ROLES: tuple[tuple[str, str], ...] = (
    ("工程部", "维修技工"),
    ("客服部", "客服专员"),
    ("秩序部", "秩序主管"),
)

PROJECT_TITLE = "项目档案"
STAFF_TITLE = "人员档案"
WORK_ORDER_TITLE = "工单汇总查询"

PROJECT_COLUMNS = (
    "项目编号",
    "项目名称",
    "城市",
    "所属区域",
    "项目地址",
    "建筑面积",
    "交付年份",
    "业态类型",
    "物业经理",
)
STAFF_COLUMNS = (
    "人员编号",
    "姓名",
    "所属项目编号",
    "部门",
    "岗位",
    "联系电话",
    "在职状态",
)
WORK_ORDER_FK_COLUMNS = ("项目编号", "接单人编号", "维修人员编号")
WORK_ORDER_ALL_COLUMNS = REPORT_COLUMNS + WORK_ORDER_FK_COLUMNS


def generate_mock_tables(output_dir: Path) -> dict[str, Any]:
    """生成项目表、人员表、工单表及 manifest.json。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(MOCK_SEED)

    projects = pd.DataFrame(PROJECTS, columns=list(PROJECT_COLUMNS))
    staff = _build_staff_frame(projects)
    projects["物业经理"] = [
        _manager_for(project_id, staff) for project_id in projects["项目编号"]
    ]
    orders = _build_work_order_frame(rng, projects, staff)

    _validate_mock_integrity(projects, staff, orders)

    manifest = {
        "version": 1,
        "seed": MOCK_SEED,
        "tables": [
            {
                "name": "工单表",
                "file": "工单表.xlsx",
                "role": "fact",
                "description": "工单事实表，一行一张工单；项目编号、接单人编号、维修人员编号均为外键",
                "primary_key": ["单号"],
                "time_column": "登记时间",
                "foreign_keys": [
                    {
                        "columns": ["项目编号"],
                        "references": {"table": "项目表", "columns": ["项目编号"]},
                    },
                    {
                        "columns": ["接单人编号"],
                        "references": {"table": "人员表", "columns": ["人员编号"]},
                    },
                    {
                        "columns": ["维修人员编号"],
                        "references": {"table": "人员表", "columns": ["人员编号"]},
                    },
                ],
            },
            {
                "name": "项目表",
                "file": "项目表.xlsx",
                "role": "dimension",
                "description": "项目主数据，一行一个项目",
                "primary_key": ["项目编号"],
            },
            {
                "name": "人员表",
                "file": "人员表.xlsx",
                "role": "dimension",
                "description": "人员主数据，一行一名员工，所属项目编号指向项目表",
                "primary_key": ["人员编号"],
                "foreign_keys": [
                    {
                        "columns": ["所属项目编号"],
                        "references": {"table": "项目表", "columns": ["项目编号"]},
                    }
                ],
            },
        ],
    }

    write_table_workbook(
        orders,
        output_dir / "工单表.xlsx",
        title=WORK_ORDER_TITLE,
        column_widths=REPORT_COLUMN_WIDTHS,
    )
    write_table_workbook(
        projects,
        output_dir / "项目表.xlsx",
        title=PROJECT_TITLE,
    )
    write_table_workbook(
        staff,
        output_dir / "人员表.xlsx",
        title=STAFF_TITLE,
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "output_dir": str(output_dir),
        "tables": {
            "工单表": len(orders),
            "项目表": len(projects),
            "人员表": len(staff),
        },
        "projects": len(projects),
        "staff": len(staff),
        "work_orders": len(orders),
    }


def _build_staff_frame(projects: pd.DataFrame) -> pd.DataFrame:
    """每个项目固定 3 名员工，保证人员归属与项目一一可追溯。"""
    records: list[dict[str, Any]] = []
    for project_index, project_id in enumerate(projects["项目编号"]):
        for role_index, (department, position) in enumerate(_STAFF_ROLES):
            name_index = project_index * len(_STAFF_ROLES) + role_index
            records.append(
                {
                    "人员编号": f"E{name_index + 1:03d}",
                    "姓名": _STAFF_NAMES[name_index],
                    "所属项目编号": project_id,
                    "部门": department,
                    "岗位": position,
                    "联系电话": f"138{name_index + 1:08d}",
                    "在职状态": "在职",
                }
            )
    return pd.DataFrame(records, columns=list(STAFF_COLUMNS))


def _build_work_order_frame(
    rng: random.Random,
    projects: pd.DataFrame,
    staff: pd.DataFrame,
) -> pd.DataFrame:
    """复用现有 RPA 模拟器生成 46 列明细，再补齐稳定的外键列。"""
    driver = SimulatedRpaExportDriver()
    base = driver._build_frame(  # noqa: SLF001 - Mock 生成器内部复用同一套版式
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 7),
        property_name=None,
    )
    if len(base) < WORK_ORDER_ROWS:
        raise RuntimeError(f"RPA 模拟器只生成了 {len(base)} 行，不足以构造 Mock 表")

    # 先保证每个项目至少 3 单，再用其余行补足 150 单；否则低频项目
    # 可能在随机抽样中完全缺席，导致按项目/区域查询出现误导性的空结果。
    selected: list[int] = []
    used: set[int] = set()
    for project_name in projects["项目名称"]:
        candidates = base.index[base["项目名称"] == project_name].tolist()
        if len(candidates) < 3:
            raise RuntimeError(
                f"项目 {project_name} 的工单不足 3 行，无法保证 Mock 覆盖"
            )
        for index in candidates[:3]:
            selected.append(index)
            used.add(index)

    for index in base.index:
        if len(selected) >= WORK_ORDER_ROWS:
            break
        if index not in used:
            selected.append(index)
            used.add(index)

    base = base.loc[sorted(selected[:WORK_ORDER_ROWS])].copy().reset_index(drop=True)

    project_by_name = {
        row["项目名称"]: row["项目编号"] for _, row in projects.iterrows()
    }
    staff_by_project: dict[str, list[dict[str, Any]]] = {}
    for record in staff.to_dict(orient="records"):
        staff_by_project.setdefault(str(record["所属项目编号"]), []).append(record)

    rows: list[dict[str, Any]] = []
    for index, record in enumerate(base.to_dict(orient="records")):
        project_name = str(record["项目名称"]).strip()
        project_id = project_by_name.get(project_name)
        if project_id is None:
            raise RuntimeError(f"工单引用了不存在的项目: {project_name}")
        project_staff = staff_by_project[project_id]
        assignee = project_staff[index % len(project_staff)]
        repairer = project_staff[(index + 1) % len(project_staff)]

        # 名称与编号必须同时改写，避免模型看到“姓名和编号对不上”的脏数据。
        record["项目编号"] = project_id
        record["接单人"] = assignee["姓名"]
        record["接单人编号"] = assignee["人员编号"]
        record["维修人员"] = repairer["姓名"]
        record["维修人员编号"] = repairer["人员编号"]
        if not str(record.get("当前节点人", "")).strip():
            record["当前节点人"] = assignee["姓名"]
        rows.append(record)

    frame = pd.DataFrame(rows, columns=list(WORK_ORDER_ALL_COLUMNS))
    return frame.sort_values("登记时间", ascending=False, kind="stable").reset_index(
        drop=True
    )


def _manager_for(project_id: str, staff: pd.DataFrame) -> str:
    project_staff = staff.loc[staff["所属项目编号"] == project_id].reset_index(
        drop=True
    )
    if project_staff.empty:
        raise RuntimeError(f"项目 {project_id} 没有员工")
    return str(project_staff.iloc[0]["姓名"])


def _validate_mock_integrity(
    projects: pd.DataFrame,
    staff: pd.DataFrame,
    orders: pd.DataFrame,
) -> None:
    """在写文件前做硬性断言，任何关联断裂都直接失败。"""
    project_ids = set(projects["项目编号"].astype(str))
    staff_ids = set(staff["人员编号"].astype(str))
    staff_project = dict(
        zip(staff["人员编号"].astype(str), staff["所属项目编号"].astype(str))
    )

    if not projects["项目编号"].is_unique:
        raise AssertionError("项目编号必须唯一")
    if not projects["项目名称"].is_unique:
        raise AssertionError("项目名称必须唯一")
    if not staff["人员编号"].is_unique:
        raise AssertionError("人员编号必须唯一")
    if not staff["姓名"].is_unique:
        raise AssertionError("人员姓名必须唯一")
    if not orders["单号"].is_unique:
        raise AssertionError("工单单号必须唯一")

    if not set(staff["所属项目编号"].astype(str)).issubset(project_ids):
        raise AssertionError("人员表存在悬空的项目外键")
    if not set(orders["项目编号"].astype(str)).issubset(project_ids):
        raise AssertionError("工单表存在悬空的项目外键")
    if not set(orders["接单人编号"].astype(str)).issubset(staff_ids):
        raise AssertionError("工单表存在悬空的接单人员工外键")
    if not set(orders["维修人员编号"].astype(str)).issubset(staff_ids):
        raise AssertionError("工单表存在悬空的维修人员工外键")

    for row in orders[["项目编号", "接单人编号", "维修人员编号"]].itertuples(
        index=False
    ):
        project_id, assignee_id, repairer_id = (str(value) for value in row)
        if staff_project.get(assignee_id) != project_id:
            raise AssertionError(f"接单人 {assignee_id} 不属于项目 {project_id}")
        if staff_project.get(repairer_id) != project_id:
            raise AssertionError(f"维修人员 {repairer_id} 不属于项目 {project_id}")

    if len(orders) != WORK_ORDER_ROWS:
        raise AssertionError(f"工单行数应为 {WORK_ORDER_ROWS}，实际 {len(orders)}")


def write_table_workbook(
    frame: pd.DataFrame,
    target: Path,
    *,
    title: str,
    column_widths: dict[str, float] | None = None,
    sheet_name: str = REPORT_SHEET_NAME,
) -> None:
    """按现有导出件的版式写表：标题行 + 表头行 + 明细。"""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name

    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    header_fill = PatternFill(fill_type="solid", fgColor="D9D9D9")
    header_alignment = Alignment(horizontal="center", vertical="center")
    data_alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    header_font = Font(name="Calibri", size=11)
    data_font = Font(name="Calibri", size=11)

    columns = [str(column) for column in frame.columns]
    last_column = get_column_letter(len(columns))
    worksheet.merge_cells(f"A1:{last_column}1")
    title_cell = worksheet["A1"]
    title_cell.value = title
    title_cell.font = Font(name="Calibri", size=16, bold=True)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    worksheet.row_dimensions[1].height = 30

    for column_index, column_name in enumerate(columns, start=1):
        cell = worksheet.cell(row=2, column=column_index, value=column_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border
        width = (column_widths or {}).get(column_name)
        if width is None:
            width = max(12.0, min(30.0, len(column_name) * 2.0 + 4.0))
        worksheet.column_dimensions[get_column_letter(column_index)].width = width
    worksheet.row_dimensions[2].height = 20

    for row_index, record in enumerate(frame.to_dict(orient="records"), start=3):
        for column_index, column_name in enumerate(columns, start=1):
            value = record.get(column_name)
            cell = worksheet.cell(
                row=row_index,
                column=column_index,
                value=_excel_value(value),
            )
            cell.font = data_font
            cell.alignment = data_alignment
            cell.border = thin_border
        worksheet.row_dimensions[row_index].height = 20

    workbook.properties.creator = "Apache POI"
    workbook.save(target)


def _excel_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value
