"""RPA 浏览器驱动的可替换实现。

第一阶段使用 ``SimulatedRpaExportDriver`` 生成与真实「工单汇总查询」
导出件一致的 Excel（标题行 + 46 列业务表头 + 明细数据），以验证
「下载文件 -> Pandas -> 汇总 -> 网关 -> 看板」全链路。

接入真实系统时，新建 ``PlaywrightRpaExportDriver`` 并实现同一 Protocol，
在构造 RPAFetcher 时注入即可；RPAFetcher 无需修改。
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.adapters.base import FetchRequest
from app.adapters.report_schema import (
    REPORT_COLUMN_WIDTHS,
    REPORT_COLUMNS,
    REPORT_SHEET_NAME,
    REPORT_TITLE,
)

logger = logging.getLogger(__name__)


class RpaExportDriver(Protocol):
    """RPA 下载动作抽象。"""

    def download_work_orders(self, request: FetchRequest, download_dir: Path) -> Path:
        """执行页面登录、筛选、导出，并返回下载到本地的 Excel 路径。"""
        ...


class SimulatedRpaExportDriver:
    """仅用于第一阶段的 RPA 模拟器。

    生成的数据按真实导出件的 46 列结构组织，并保留常见的缺失值、
    状态流转、告警工单占比和回访信息，便于直接用真实浏览器自动化替换。
    """

    # 项目及其单号前缀、业务量占比。占比参考真实导出件。
    _PROJECTS: tuple[tuple[str, str, float], ...] = (
        ("望京国际研发园", "YFY", 0.625),
        ("望京生命科学园", "SMY", 0.135),
        ("望京科技园", "KJY", 0.086),
        ("望京数字创意园", "CYY", 0.083),
        ("望京未来信息园", "XXY", 0.040),
        ("慧谷时空", "HGS", 0.015),
        ("金色家园", "JSJY1", 0.010),
        ("东湖街道办事处", "DHJ", 0.007),
    )

    _TICKET_TYPES: tuple[tuple[str, int], ...] = (
        ("数智中枢告警工单", 817),
        ("提交工单", 149),
        ("住宅报单", 31),
        ("客户报事报修", 2),
        ("客户投诉建议", 1),
        ("补单", 1),
    )

    _STATUSES: tuple[tuple[str, int], ...] = (
        ("已处理", 9720),
        ("处理中", 260),
        ("草稿", 10),
        ("已作废", 10),
    )

    _PEOPLE = (
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
    )
    _COMPANIES = (
        "北京云启科技有限公司",
        "北京星河数据有限公司",
        "北京远山智能科技有限公司",
        "北京清源环境有限公司",
        "北京博远信息技术有限公司",
        "北京新辰文化有限公司",
    )
    _BUILDINGS = (
        "1号楼",
        "2号楼",
        "3号楼",
        "A座",
        "B座",
        "C座",
        "D座",
        "E座",
        "F座",
        "G座",
    )
    _LOCATIONS = (
        "1层东侧",
        "1层西侧",
        "2层走廊",
        "3层电梯厅",
        "B1车库东门",
        "B1配电室",
        "南门",
        "北广场",
        "中控室",
        "消防通道",
    )

    def __init__(self, simulate_delay_seconds: float = 0.0) -> None:
        self._simulate_delay_seconds = simulate_delay_seconds

    def download_work_orders(self, request: FetchRequest, download_dir: Path) -> Path:
        if self._simulate_delay_seconds:
            # 模拟浏览器打开、登录、点击筛选和下载的耗时。
            time.sleep(self._simulate_delay_seconds)

        download_dir.mkdir(parents=True, exist_ok=True)
        time_range = request.time_range
        file_name = (
            f"property_work_orders_{time_range.start_date.isoformat()}_"
            f"{time_range.end_date.isoformat()}_{uuid.uuid4().hex[:8]}.xlsx"
        )
        target = download_dir / file_name

        frame = self._build_frame(
            start_date=time_range.start_date,
            end_date=time_range.end_date,
            property_name=request.property_name,
        )
        _write_report_workbook(frame, target)

        logger.info("RPA 模拟下载完成: %s, rows=%s", target.name, len(frame))
        return target

    def _build_frame(
        self,
        *,
        start_date: date,
        end_date: date,
        property_name: str | None,
    ) -> pd.DataFrame:
        """生成可复现的报表数据，同一查询条件多次执行结果保持稳定。"""
        seed = f"{start_date.isoformat()}:{end_date.isoformat()}:{property_name or '*'}"
        rng = random.Random(seed)
        records: list[dict[str, str]] = []
        current = start_date

        while current <= end_date:
            daily_count = self._daily_count(rng, property_name)
            sequence: defaultdict[str, int] = defaultdict(int)
            for _ in range(daily_count):
                records.append(
                    self._build_record(
                        rng=rng,
                        current=current,
                        property_name=property_name,
                        sequence=sequence,
                    )
                )
            current += timedelta(days=1)

        frame = pd.DataFrame.from_records(records, columns=list(REPORT_COLUMNS))
        # 真实系统按登记时间倒序导出，这里保持同样的阅读顺序。
        return frame.sort_values(
            "登记时间", ascending=False, kind="stable"
        ).reset_index(drop=True)

    def _daily_count(self, rng: random.Random, property_name: str | None) -> int:
        """按真实导出件的日均单量生成每天的数据量。"""
        base_count = rng.randint(116, 146)
        if not property_name:
            return base_count

        project_meta = self._project_meta(property_name)
        if project_meta is None:
            # 兼容自定义项目名：生成少量数据，避免查询结果为空。
            return rng.randint(3, 5)
        return max(1, round(base_count * project_meta[2]))

    def _build_record(
        self,
        *,
        rng: random.Random,
        current: date,
        property_name: str | None,
        sequence: defaultdict[str, int],
    ) -> dict[str, str]:
        ticket_type = _weighted_choice(rng, self._TICKET_TYPES)
        project = property_name or _weighted_project(rng, self._PROJECTS)
        is_alarm = ticket_type == "数智中枢告警工单"
        status = _weighted_choice(rng, self._STATUSES)

        received_at = _random_datetime(rng, current)
        row: dict[str, str] = {column: "" for column in REPORT_COLUMNS}
        row["单号"] = self._ticket_id(
            current=current,
            project=project,
            ticket_type=ticket_type,
            sequence=sequence,
        )
        row["登记时间"] = _format_datetime(received_at)
        row["项目名称"] = project
        row["工单类型"] = ticket_type
        row["处理状态"] = status
        row["流程名称"] = _flow_name(ticket_type)
        row["登记人"] = "Admin" if is_alarm else rng.choice(self._PEOPLE)
        row["紧急程度"] = (
            "紧急" if is_alarm else ("普通" if rng.random() < 0.88 else "紧急")
        )
        row["数据来源"] = _data_source(rng, is_alarm)
        row["户内公区"] = _indoor_or_public(rng, ticket_type)
        row["任务时间"] = _format_datetime(
            received_at + timedelta(minutes=rng.randint(0, 5))
        )
        row["当前节点人"] = rng.choice(self._PEOPLE)

        service_kind = self._fill_service_fields(rng, row, ticket_type)
        row["问题描述"] = self._problem_description(
            rng=rng,
            project=project,
            ticket_type=ticket_type,
            service_kind=service_kind,
        )
        self._fill_customer_fields(rng, row, project, ticket_type)

        completed_at = self._fill_workflow(
            rng=rng,
            row=row,
            status=status,
            is_alarm=is_alarm,
            service_kind=service_kind,
            received_at=received_at,
        )
        self._fill_outcome(
            rng=rng,
            row=row,
            status=status,
            ticket_type=ticket_type,
            service_kind=service_kind,
            completed_at=completed_at,
        )
        self._fill_manager_times(rng, row, service_kind, received_at)
        self._fill_appointment(rng, row, ticket_type, received_at)

        return row

    def _ticket_id(
        self,
        *,
        current: date,
        project: str,
        ticket_type: str,
        sequence: defaultdict[str, int],
    ) -> str:
        """按真实规则生成单号：告警用日期流水，其余用项目前缀流水。"""
        if ticket_type == "数智中枢告警工单":
            sequence["alarm"] += 1
            return f"{current:%Y%m%d}-{sequence['alarm']:03d}"

        prefix = (
            self._project_meta(project)[1] if self._project_meta(project) else "GEN"
        )
        letter = "B" if ticket_type == "补单" else "G"
        key = f"{prefix}-{letter}"
        sequence[key] += 1
        return f"{prefix}-{current:%Y%m%d}-{letter}-{sequence[key]:03d}"

    @classmethod
    def _project_meta(cls, project: str) -> tuple[str, str, float] | None:
        for name, prefix, weight in cls._PROJECTS:
            if name == project:
                return name, prefix, weight
        return None

    def _fill_service_fields(
        self,
        rng: random.Random,
        row: dict[str, str],
        ticket_type: str,
    ) -> str:
        """填写服务项目/服务内容，并返回工程或其他服务大类。"""
        if ticket_type in ("数智中枢告警工单", "补单"):
            return "其他服务"

        service_kind = "工程维修" if rng.random() < 0.96 else "其他服务"
        project_label = {
            "住宅报单": "住宅报单",
            "客户报事报修": "客户报事报修",
            "客户投诉建议": "客户报事报修",
        }.get(ticket_type, "提交工单")
        row["工单服务项目"] = f"【服务项目】{project_label}/{service_kind}"

        if service_kind == "工程维修" and rng.random() < 0.85:
            path = rng.choice(
                (
                    "强电专业维修/灯具维修/更换节能灯",
                    "给排水维修/阀门维修/其他类维修",
                    "空调维修/末端制冷设备维修/其他类维修",
                    "土建装饰/土建类/墙面抹灰",
                    "综合专业维修/门锁配件/闭门器安装/维修",
                )
            )
            row["服务内容"] = f"【业务类型】{project_label}/工程维修/{path}[]"
        elif service_kind == "其他服务" and rng.random() < 0.55:
            row["服务内容"] = (
                f"【业务类型】{project_label}/其他服务/其他服务/其他类/其他服务[]"
            )
        return service_kind

    def _problem_description(
        self,
        *,
        rng: random.Random,
        project: str,
        ticket_type: str,
        service_kind: str,
    ) -> str:
        if ticket_type == "数智中枢告警工单":
            device_type = rng.choice(
                (
                    "安防摄像头",
                    "周界摄像头",
                    "出入口摄像头",
                    "电梯摄像头",
                    "机房门禁通道",
                )
            )
            building = rng.choice(self._BUILDINGS)
            location = rng.choice(self._LOCATIONS)
            camera = rng.choice(("球机", "枪机", "半球"))
            alarm_content = rng.choice(
                ("通道断线", "人穿越区域", "翻越围栏", "超时门未关", "区域入侵")
            )
            description = (
                f"设备类型：{device_type}；设备名称：{project}{building}{location}-{camera}；"
                f"告警内容：{alarm_content}"
            )
            if rng.random() < 0.25:
                description += "\n发起备注：自动发起告警工单"
            return description

        issue = rng.choice(
            (
                "照明灯具不亮",
                "下水管道堵塞",
                "门锁损坏",
                "墙面起皮脱落",
                "空调制冷效果差",
                "卫生间水龙头漏水",
                "疏散指示牌松动",
                "插座无电",
                "玻璃门闭门器异响",
                "消防通道堆放杂物",
            )
            if service_kind == "工程维修"
            else (
                "公共区域清洁不及时",
                "客户咨询停车办理",
                "前台访客登记异常",
                "电话无法接通",
                "会议室设备使用咨询",
            )
        )
        if ticket_type == "客户投诉建议":
            return rng.choice(
                (
                    "客户反映停车收费问题，请核实处理",
                    "客户建议增加园区班车班次",
                    "客户投诉施工噪音",
                )
            )
        if ticket_type == "补单":
            return "换热站补水泵报警，补录工单"
        return f"{rng.choice(self._BUILDINGS)}{rng.choice(self._LOCATIONS)}{issue}"

    def _fill_customer_fields(
        self,
        rng: random.Random,
        row: dict[str, str],
        project: str,
        ticket_type: str,
    ) -> None:
        if ticket_type == "数智中枢告警工单":
            return

        probability = 0.75 if ticket_type in ("客户报事报修", "客户投诉建议") else 0.12
        if rng.random() >= probability:
            return

        row["客户名称"] = rng.choice(self._COMPANIES)
        row["房间号"] = (
            f"{project}/{rng.choice(self._BUILDINGS)}/"
            f"{rng.choice(('2F', '3F', '5F', '8F', '12F'))}(出租)"
        )
        row["联系人"] = rng.choice(self._PEOPLE)
        row["联系电话"] = _phone_number(rng)

    def _fill_workflow(
        self,
        *,
        rng: random.Random,
        row: dict[str, str],
        status: str,
        is_alarm: bool,
        service_kind: str,
        received_at: datetime,
    ) -> datetime | None:
        """按真实数据风格补齐接单、到场、完成时间，并返回完成时间。

        真实导出件中只有少部分工单保留完整流程时间，因此这里也保留
        较高的缺失比例，避免模拟数据过于理想化。
        """
        if status not in ("已处理", "处理中"):
            return None

        has_flow = False
        if status == "已处理":
            has_flow = rng.random() < (0.18 if is_alarm else 0.92)
        elif rng.random() < 0.55:
            has_flow = True

        if not has_flow:
            return None

        accepted_at = received_at + timedelta(minutes=rng.randint(1, 20))
        arrival_at = accepted_at + timedelta(minutes=rng.randint(1, 30))
        completed_at: datetime | None = None
        if status == "已处理":
            handling_minutes = rng.randint(5, 180 if is_alarm else 480)
            completed_at = arrival_at + timedelta(minutes=handling_minutes)

        row["接单人"] = rng.choice(self._PEOPLE)
        row["维修人员"] = rng.choice(self._PEOPLE)
        row["收单时间"] = _format_datetime(accepted_at)
        row["接单时间"] = _format_datetime(accepted_at)
        row["到场时间"] = _format_datetime(arrival_at)
        row["接单时效"] = str(
            max(0, int((accepted_at - received_at).total_seconds() // 60))
        )
        row["到达现场时效"] = str(
            max(0, int((arrival_at - accepted_at).total_seconds() // 60))
        )
        if completed_at:
            row["完成时间"] = _format_datetime(completed_at)
            row["处理时效"] = str(
                max(0, int((completed_at - arrival_at).total_seconds() // 60))
            )
        if service_kind == "工程维修" and rng.random() < 0.25:
            row["使用材料"] = (
                f"({rng.randint(1, 4):.2f}){rng.choice(('个', '套', '支', '包'))};"
            )
        return completed_at

    def _fill_outcome(
        self,
        *,
        rng: random.Random,
        row: dict[str, str],
        status: str,
        ticket_type: str,
        service_kind: str,
        completed_at: datetime | None,
    ) -> None:
        if status == "已处理":
            row["当前节点"] = "结束"
            row["处理内容"] = _handling_result(rng, ticket_type, service_kind)
        elif status == "处理中":
            row["当前节点"] = rng.choice(
                ("处理", "项目安保经理", "工程经理", "回访", "经理复核")
            )
            if rng.random() < 0.45:
                row["处理内容"] = _handling_result(rng, ticket_type, service_kind)
            if rng.random() < 0.35:
                row["未处理"] = row["当前节点"]
        elif status == "草稿":
            row["当前节点"] = "起草"
        else:
            row["当前节点"] = "撤销"

        if completed_at and rng.random() < 0.11:
            row["满意度"] = "非常满意" if rng.random() < 0.9 else "满意"

        if row["满意度"] and rng.random() < 0.65:
            row["是否回访"] = "是"
            row["回访方式"] = rng.choice(("电话", "上门", "其他"))
            row["回访人员"] = rng.choice(self._PEOPLE)
            row["回访时间"] = _format_datetime(
                completed_at + timedelta(minutes=rng.randint(30, 60 * 24 * 2))
            )

    def _fill_manager_times(
        self,
        rng: random.Random,
        row: dict[str, str],
        service_kind: str,
        received_at: datetime,
    ) -> None:
        if service_kind != "工程维修" or rng.random() >= 0.7:
            return
        row["物业经理派单时间"] = _format_minute(
            received_at + timedelta(minutes=rng.randint(2, 30))
        )
        row["工程经理派单时间"] = _format_minute(
            received_at + timedelta(minutes=rng.randint(5, 60))
        )

    def _fill_appointment(
        self,
        rng: random.Random,
        row: dict[str, str],
        ticket_type: str,
        received_at: datetime,
    ) -> None:
        if ticket_type == "数智中枢告警工单" or rng.random() >= 0.02:
            return
        row["预约时间"] = _format_datetime(
            received_at + timedelta(hours=rng.randint(1, 48), minutes=0, seconds=0)
        )


def _write_report_workbook(frame: pd.DataFrame, target: Path) -> None:
    """按真实导出件的版式写入 Excel。"""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = REPORT_SHEET_NAME

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

    last_column = get_column_letter(len(REPORT_COLUMNS))
    worksheet.merge_cells(f"A1:{last_column}1")
    title_cell = worksheet["A1"]
    title_cell.value = REPORT_TITLE
    title_cell.font = Font(name="Calibri", size=16, bold=True)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    worksheet.row_dimensions[1].height = 30

    for column_index, column_name in enumerate(REPORT_COLUMNS, start=1):
        cell = worksheet.cell(row=2, column=column_index, value=column_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border
        worksheet.column_dimensions[get_column_letter(column_index)].width = (
            REPORT_COLUMN_WIDTHS.get(column_name, 18.75)
        )
    worksheet.row_dimensions[2].height = 20

    for row_index, record in enumerate(frame.to_dict(orient="records"), start=3):
        for column_index, column_name in enumerate(REPORT_COLUMNS, start=1):
            value = record.get(column_name)
            cell = worksheet.cell(
                row=row_index,
                column=column_index,
                value=None if value in (None, "") else value,
            )
            cell.font = data_font
            cell.alignment = data_alignment
            cell.border = thin_border
        worksheet.row_dimensions[row_index].height = 20

    workbook.properties.creator = "Apache POI"
    workbook.save(target)


def _weighted_choice(rng: random.Random, options: tuple[tuple[str, int], ...]) -> str:
    values = [value for value, _ in options]
    weights = [weight for _, weight in options]
    return rng.choices(values, weights=weights, k=1)[0]


def _weighted_project(
    rng: random.Random, projects: tuple[tuple[str, str, float], ...]
) -> str:
    values = [name for name, _, _ in projects]
    weights = [weight for _, _, weight in projects]
    return rng.choices(values, weights=weights, k=1)[0]


def _flow_name(ticket_type: str) -> str:
    return {
        "数智中枢告警工单": "安防告警工单",
        "提交工单": "报单",
        "住宅报单": "住宅报单",
        "客户报事报修": "客户报事报修",
        "客户投诉建议": "客户投诉建议",
        "补单": "补单",
    }[ticket_type]


def _data_source(rng: random.Random, is_alarm: bool) -> str:
    if is_alarm:
        return "其他"
    return rng.choices(
        ("来电", "内部APP", "微信", "来访"),
        weights=(60, 32, 6, 2),
        k=1,
    )[0]


def _indoor_or_public(rng: random.Random, ticket_type: str) -> str:
    if ticket_type == "数智中枢告警工单":
        return ""
    return rng.choices(("公区", "租区", "机房", "户内"), weights=(72, 13, 13, 2), k=1)[
        0
    ]


def _random_datetime(rng: random.Random, current: date) -> datetime:
    """登记时间集中在工作时段，并保留真实数据中的午间小高峰。"""
    hour = rng.choices(
        (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19),
        weights=(10, 18, 14, 9, 4, 6, 10, 12, 11, 8, 5, 3),
        k=1,
    )[0]
    return datetime.combine(
        current,
        dt_time(
            hour=hour,
            minute=rng.randint(0, 59),
            second=rng.randint(0, 59),
        ),
    )


def _format_datetime(value: datetime) -> str:
    """真实导出件的时间是带 .0 的字符串。"""
    return value.strftime("%Y-%m-%d %H:%M:%S") + ".0"


def _format_minute(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _phone_number(rng: random.Random) -> str:
    if rng.random() < 0.35:
        return f"8{rng.randint(0, 99):02d}"
    return f"1380000{rng.randint(0, 9999):04d}"


def _handling_result(
    rng: random.Random,
    ticket_type: str,
    service_kind: str,
) -> str:
    if ticket_type == "数智中枢告警工单":
        return rng.choice(
            (
                "现场查看为误报，无异常",
                "监控硬盘损坏，已上报工安部待更换",
                "通道断线，已恢复",
                "已通知安保人员现场核实",
                "设备重启后恢复正常",
            )
        )
    if service_kind == "工程维修":
        return rng.choice(
            (
                "已修复",
                "已更换配件",
                "现场处理完成",
                "已疏通，恢复正常使用",
                "已联系厂家，等待配件到货",
            )
        )
    return rng.choice(("已处理", "已电话回复客户", "已协调相关部门跟进", "已完成"))
