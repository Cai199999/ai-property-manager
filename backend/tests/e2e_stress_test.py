#!/usr/bin/env python3
"""端到端（E2E）压力测试：通过 Java 网关验证自然语言 -> Pandas 代码生成能力。

测试固定走生产链路：脚本 -> Java 网关 8080 -> Python Agent 8000。
20 个请求按“单表统计 / 多表合并 / 时间差 / 图表 / 模糊口语化”五类编排，
每个请求都携带真实 ``X-Request-Id``，并记录：

* HTTP 状态码与响应时间（RT）；
* 响应 JSON 的必填字段是否完整（result / chart_meta / download_url / code / meta）；
* 结果列、行数、关键数值是否符合源数据口径；
* AI 自修复迹象（attempts > 1、retry_errors）与后端警告。

用法：
    cd backend && ../.venv/bin/python tests/e2e_stress_test.py
    cd backend && ../.venv/bin/python tests/e2e_stress_test.py --no-download-check
    cd backend && ../.venv/bin/python tests/e2e_stress_test.py --only 6,7,14

注意：本文件以 ``_test.py`` 结尾，会被 pytest 默认规则收集；``__test__ = False``
用于显式关闭收集，避免无意中在单元测试里触发 20 次模型调用。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

__test__ = False

GATEWAY_BASE = "http://127.0.0.1:8080"
AGENT_BASE = "http://127.0.0.1:8000"
API_PREFIX = "/api/v1/assistant"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backend" / ".runtime" / "e2e_stress"

# 成功响应必须具备的顶层字段；download_url / word_download_url 允许为 null。
REQUIRED_SUCCESS_KEYS = (
    "request_id",
    "result",
    "chart_meta",
    "download_url",
    "word_download_url",
    "code",
    "meta",
)

# 失败归因优先级：数字越小越先展示。
ATTRIBUTION_ORDER = (
    "传输异常",
    "意图识别：时间范围",
    "Pandas 代码生成/执行",
    "结果为空",
    "意图识别：图表类型",
    "语义理解：结果列",
    "数据口径：数值不符",
    "响应结构",
    "导出链路",
    "未知",
)


@dataclass(frozen=True)
class Case:
    """一条测试用例及其可验证的期望。"""

    category: str
    message: str
    note: str = ""
    # 期望图表类型；None 表示不校验。
    chart: str | None = None
    # 期望行数闭区间；仅作为“形态提示”，不判失败。
    row_range: tuple[int, int] | None = None
    # 每组子串至少命中一个结果列，否则判失败。
    columns: tuple[tuple[str, ...], ...] = ()
    # 结果中任意数值单元格应命中的期望值：(期望值, 容差)。
    values: tuple[tuple[float, float], ...] = ()
    # 指定列（按子串匹配）求和应命中的期望值：(列名子串, 期望值, 容差)。
    sums: tuple[tuple[str, float, float], ...] = ()
    # 该用例允许返回空结果（例如数据缺口属于合法业务边界）。
    allow_empty: bool = False


CASES: tuple[Case, ...] = (
    # ── 单表统计 ─────────────────────────────────────────────────────────
    Case(
        "单表统计",
        "统计9月各处理状态的工单数量",
        note="期望 3 行：已处理 898 / 处理中 20 / 已作废 1，合计 919",
        columns=(("处理状态",),),
        row_range=(3, 4),
        values=((898.0, 0.0), (20.0, 0.0), (1.0, 0.0)),
        sums=(("工单", 919.0, 0.0),),
    ),
    Case(
        "单表统计",
        "9月各工单类型分别有多少单？按数量从高到低排序",
        note="期望 4 行：761 / 122 / 30 / 6",
        columns=(("工单类型",),),
        row_range=(4, 4),
        values=((761.0, 0.0), (122.0, 0.0), (30.0, 0.0), (6.0, 0.0)),
        sums=(("工单", 919.0, 0.0),),
    ),
    Case(
        "单表统计",
        "9月份各项目的工单量排行",
        note="期望 8 行，首名望京国际研发园 331",
        columns=(("项目",),),
        row_range=(8, 8),
        values=((331.0, 0.0), (283.0, 0.0), (37.0, 0.0)),
        sums=(("工单", 919.0, 0.0),),
    ),
    Case(
        "单表统计",
        "9月紧急工单有多少条？占总量的比例是多少？",
        note="期望紧急工单 777 条（占比约 84.55%）",
        row_range=(1, 2),
        values=((777.0, 0.0),),
    ),
    Case(
        "单表统计",
        "按维修人员统计9月已处理工单数量，取前10名",
        note="期望结果不超过 10 行，含维修人员列",
        columns=(("维修人员",), ("工单", "数量")),
        row_range=(1, 10),
    ),
    # ── 多表合并求和 ─────────────────────────────────────────────────────
    Case(
        "多表合并",
        "汇总A区、B区、C区所有工单的总工单量和总费用",
        note="正确口径（A/B/C 各一份）：919 单、费用 196255.21；重复上传件会放大费用",
        columns=(("工单",), ("费用",)),
        row_range=(1, 1),
        values=((919.0, 0.0),),
        sums=(("工单", 919.0, 0.0), ("费用", 196255.21, 20.0)),
    ),
    Case(
        "多表合并",
        "把A区、B区、C区的数据合起来，按项目统计工单量和费用总额",
        note="正确口径：8 个项目、工单量合计 919、费用合计 196255.21",
        columns=(("项目",), ("费用",)),
        row_range=(8, 8),
        values=((331.0, 0.0),),
        sums=(("工单", 919.0, 0.0), ("费用", 196255.21, 20.0)),
    ),
    Case(
        "多表合并",
        "A区、B区、C区合并后，各工单类型的平均费用是多少？",
        note="期望 4 行，含工单类型与平均费用列",
        columns=(("工单类型",), ("平均", "费用")),
        row_range=(4, 4),
    ),
    Case(
        "多表合并",
        "合并全部工单表，统计紧急工单有多少条、总费用和平均费用",
        note="正确口径：紧急工单 777 条、费用 18033.53、均值 23.21；重复表会放大总额",
        columns=(("费用",),),
        row_range=(1, 3),
        values=((777.0, 0.0), (18033.53, 1.0), (23.21, 0.1)),
    ),
    # ── 复杂时间差计算 ───────────────────────────────────────────────────
    Case(
        "时间差",
        "计算每个项目从登记到完成的平均处理时长（小时）",
        note="期望 8 行，含项目与时长列；源数据仅 280 条有完成时间",
        columns=(("项目",), ("时长", "耗时", "小时")),
        row_range=(1, 9),
    ),
    Case(
        "时间差",
        "统计从接单到到场的平均间隔（分钟），按工单类型排序",
        note="期望 4 行，含工单类型与分钟/间隔列",
        columns=(("工单类型",), ("分钟", "间隔", "时长", "耗时")),
        row_range=(1, 5),
    ),
    Case(
        "时间差",
        "找出从登记到完成处理时长超过4小时的工单数量，并按项目分组",
        note="按登记→完成口径应合计 72 条；若误用处理时效(分钟)会偏离",
        columns=(("项目",), ("工单", "数量")),
        row_range=(1, 8),
        sums=(("工单", 72.0, 0.0),),
    ),
    Case(
        "时间差",
        "按周统计9月工单量的趋势，用折线图展示",
        note="期望 5 个自然周、合计 919、chart=line",
        chart="line",
        columns=(("周",), ("工单",)),
        row_range=(4, 6),
        sums=(("工单", 919.0, 0.0),),
    ),
    # ── 图表要求 ─────────────────────────────────────────────────────────
    Case(
        "图表要求",
        "用饼图展示各项目工单量的占比",
        note="显式要求饼图；若结果含 2 个数值列会被降级为柱状图",
        chart="pie",
        columns=(("项目",), ("工单",)),
        row_range=(1, 8),
        values=((331.0, 0.0),),
        sums=(("工单", 919.0, 0.0),),
    ),
    Case(
        "图表要求",
        "用折线图展示9月每天工单量的变化趋势",
        note="期望 30 天、合计 919、chart=line",
        chart="line",
        columns=(("日期", "天", "日"), ("工单",)),
        row_range=(25, 31),
        sums=(("工单", 919.0, 0.0),),
    ),
    Case(
        "图表要求",
        "用柱状图对比各工单类型的费用总额",
        note="期望 4 行、chart=bar、费用合计 196255.21（重复表会放大）",
        chart="bar",
        columns=(("工单类型",), ("费用",)),
        row_range=(1, 4),
        sums=(("费用", 196255.21, 20.0),),
    ),
    # ── 模糊口语化 ───────────────────────────────────────────────────────
    Case(
        "模糊口语",
        "最近咱们这边活儿多不多？哪个小区最闹心？",
        note="考察意图补全：应给出工单量与项目对比",
        columns=(("项目",),),
        row_range=(1, 20),
    ),
    Case(
        "模糊口语",
        "9月份有没有什么异常情况？",
        note="开放问题：合理做法是给出分布/极值，不应空结果",
        row_range=(1, 50),
    ),
    Case(
        "模糊口语",
        "帮我看下兄弟们9月都忙成啥样了",
        note="考察“兄弟们”能否映射到维修人员/项目维度",
        row_range=(1, 50),
    ),
    Case(
        "模糊口语",
        "上个月整体情况咋样，给我个总结",
        note="考察多指标汇总；上个月=2026-09",
        row_range=(1, 20),
    ),
)


@dataclass
class Sample:
    """一次请求的采集结果。"""

    index: int
    case: Case
    request_id: str
    status: int | None = None
    rt_ms: float = 0.0
    payload: dict[str, Any] | None = None
    transport_error: str | None = None
    # 阻塞性问题：(类别, 描述)
    blocking: list[tuple[str, str]] = field(default_factory=list)
    # 非阻塞提示
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retry_errors: list[str] = field(default_factory=list)
    attempts: int = 0
    chart_type: str | None = None
    row_count: int | None = None
    download_ok: bool | None = None
    word_ok: bool | None = None
    generated_code: str | None = None
    log_evidence: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == 200 and not self.blocking

    @property
    def attribution(self) -> str:
        if not self.blocking:
            return "通过"
        kinds = {kind for kind, _ in self.blocking}
        for candidate in ATTRIBUTION_ORDER:
            if candidate in kinds:
                return candidate
        return "未知"


# ── 数值与结构校验 ──────────────────────────────────────────────────────


def _to_number(value: Any) -> float | None:
    """尽量把单元格转换为 float；bool 不算数值。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").rstrip("%")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _result_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    rows = result.get("rows")
    return rows if isinstance(rows, list) else []


def _result_columns(payload: dict[str, Any]) -> list[str]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    columns = result.get("columns")
    if not isinstance(columns, list):
        return []
    return [str(column.get("name", "")) for column in columns if isinstance(column, dict)]


def _numeric_cells(rows: list[dict[str, Any]]) -> list[float]:
    numbers: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            number = _to_number(value)
            if number is not None:
                numbers.append(number)
    return numbers


def _column_sums(rows: list[dict[str, Any]], column: str) -> float | None:
    """返回某列的数值和；列中没有任何数值时返回 None。"""
    total = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = _to_number(row.get(column))
        if number is not None:
            total += number
            found = True
    return total if found else None


def _has_expected_value(numbers: list[float], expected: float, tolerance: float) -> bool:
    return any(abs(number - expected) <= tolerance for number in numbers)


def _has_expected_column(names: list[str], group: tuple[str, ...]) -> bool:
    return any(any(keyword in name for keyword in group) for name in names)


def _is_empty_result(payload: dict[str, Any]) -> bool:
    result = payload.get("result")
    if not isinstance(result, dict):
        return True
    return int(result.get("row_count") or 0) == 0


# ── 评估逻辑 ────────────────────────────────────────────────────────────


def _check_structure(sample: Sample, payload: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_SUCCESS_KEYS if key not in payload]
    if missing:
        sample.blocking.append(("响应结构", f"缺少顶层字段: {', '.join(missing)}"))

    result = payload.get("result")
    if not isinstance(result, dict):
        sample.blocking.append(("响应结构", "result 不是对象"))
        return
    columns = result.get("columns")
    rows = result.get("rows")
    row_count = result.get("row_count")
    if not isinstance(columns, list) or not columns:
        sample.blocking.append(("响应结构", "result.columns 为空"))
    if not isinstance(rows, list):
        sample.blocking.append(("响应结构", "result.rows 不是数组"))
    elif isinstance(row_count, int) and row_count != len(rows):
        sample.blocking.append(("响应结构", f"row_count={row_count} 与实际行数 {len(rows)} 不一致"))

    code = payload.get("code")
    if not isinstance(code, dict) or "attempts" not in code:
        sample.blocking.append(("响应结构", "code.attempts 缺失"))
    meta = payload.get("meta")
    if not isinstance(meta, dict) or "warnings" not in meta:
        sample.blocking.append(("响应结构", "meta.warnings 缺失"))


def _check_chart(sample: Sample, case: Case, payload: dict[str, Any]) -> None:
    chart = payload.get("chart_meta")
    if not isinstance(chart, dict):
        sample.blocking.append(("响应结构", "chart_meta 不是对象"))
        return

    chart_type = chart.get("type")
    sample.chart_type = chart_type if isinstance(chart_type, str) else None

    names = _result_columns(payload)
    if chart_type in {"bar", "line", "pie"}:
        x = chart.get("x")
        y = chart.get("y")
        series = chart.get("series")
        if not isinstance(x, str) or x not in names:
            sample.blocking.append(("响应结构", f"chart_meta.x={x!r} 不在结果列中"))
        if not isinstance(y, list) or not y:
            sample.blocking.append(("响应结构", "chart_meta.y 为空"))
        elif any(item not in names for item in y):
            sample.blocking.append(("响应结构", f"chart_meta.y={y!r} 含未知列"))
        if not isinstance(series, list) or not series:
            sample.blocking.append(("响应结构", "chart_meta.series 为空"))

    if case.chart and chart_type != case.chart:
        sample.blocking.append(
            ("意图识别：图表类型", f"期望 {case.chart}，实际 {chart_type or '缺失'}")
        )


def _check_rows_and_columns(sample: Sample, case: Case, payload: dict[str, Any]) -> None:
    result = payload.get("result") or {}
    rows = _result_rows(payload)
    row_count = int(result.get("row_count") or 0)
    sample.row_count = row_count

    if row_count == 0 and not case.allow_empty:
        sample.blocking.append(("结果为空", "结果行数为 0"))
        return

    names = _result_columns(payload)
    for group in case.columns:
        if not _has_expected_column(names, group):
            sample.blocking.append(
                ("语义理解：结果列", f"缺少包含 {group} 的结果列；实际列={names}")
            )

    if case.row_range is not None:
        low, high = case.row_range
        if not low <= row_count <= high:
            sample.notes.append(f"行数 {row_count} 不在期望区间 [{low}, {high}]")

    numbers = _numeric_cells(rows)
    for expected, tolerance in case.values:
        if not _has_expected_value(numbers, expected, tolerance):
            sample.blocking.append(
                ("数据口径：数值不符", f"结果中未找到期望值 {expected:g}（容差 {tolerance:g}）")
            )

    for substring, expected, tolerance in case.sums:
        candidates = [name for name in names if substring in name]
        matched = False
        for column in candidates:
            total = _column_sums(rows, column)
            if total is not None and abs(total - expected) <= tolerance:
                matched = True
                break
        if not matched:
            actual = [f"{column}={_column_sums(rows, column)}" for column in candidates]
            sample.blocking.append(
                (
                    "数据口径：数值不符",
                    f"列（含“{substring}”）求和未命中 {expected:g}；实际 {actual or '无匹配列'}",
                )
            )


def _verify_link(client: httpx.Client, url: str) -> tuple[bool, str]:
    full_url = url if url.startswith("http") else f"{GATEWAY_BASE}{url}"
    try:
        response = client.get(full_url, timeout=30.0)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    if not response.content:
        return False, "空文件"
    return True, f"{len(response.content)} bytes"


def _check_exports(
    sample: Sample,
    payload: dict[str, Any],
    client: httpx.Client | None,
    check_downloads: bool,
) -> None:
    row_count = sample.row_count or 0
    download_url = payload.get("download_url")
    word_url = payload.get("word_download_url")

    if row_count > 0 and not download_url:
        sample.blocking.append(("导出链路", "有结果但 download_url 为空"))
    elif row_count == 0 and download_url:
        sample.notes.append("空结果仍返回了 download_url")

    if row_count > 0 and not word_url:
        sample.notes.append("word_download_url 为空（Word 报告降级/关闭）")

    if client is None or not check_downloads:
        return

    if isinstance(download_url, str) and download_url:
        ok, detail = _verify_link(client, download_url)
        sample.download_ok = ok
        if not ok:
            sample.blocking.append(("导出链路", f"Excel 下载失败: {detail}"))
    if isinstance(word_url, str) and word_url:
        ok, detail = _verify_link(client, word_url)
        sample.word_ok = ok
        if not ok:
            sample.notes.append(f"Word 下载失败: {detail}")


def evaluate(
    sample: Sample,
    payload: dict[str, Any],
    client: httpx.Client | None,
    check_downloads: bool,
) -> None:
    """对一次 200 响应做结构、语义与数值校验。"""
    code = payload.get("code") if isinstance(payload.get("code"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    sample.attempts = int(code.get("attempts") or 0)
    sample.retry_errors = [str(item) for item in code.get("retry_errors") or []]
    sample.warnings = [str(item) for item in meta.get("warnings") or []]
    sample.generated_code = code.get("generated_code")

    _check_structure(sample, payload)
    _check_chart(sample, sample.case, payload)
    _check_rows_and_columns(sample, sample.case, payload)
    _check_exports(sample, payload, client, check_downloads)


def classify_http_error(payload: dict[str, Any]) -> tuple[str, str]:
    """把网关错误体映射为失败类别与描述。"""
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or "UNKNOWN")
    message = str(error.get("message") or "无错误信息")
    if "时间" in message or code == "TIME_RANGE_INVALID":
        return "意图识别：时间范围", f"{code}: {message}"
    if "代码" in message or code in {"CODE_GENERATION_FAILED", "CODE_EXECUTION_TIMEOUT"}:
        return "Pandas 代码生成/执行", f"{code}: {message}"
    if "模型" in message or "LLM" in code:
        return "Pandas 代码生成/执行", f"{code}: {message}"
    return "未知", f"{code}: {message}"


# ── 执行 ────────────────────────────────────────────────────────────────


def run_case(
    index: int,
    case: Case,
    client: httpx.Client,
    run_id: str,
    check_downloads: bool,
) -> Sample:
    request_id = f"e2e-{run_id}-{index:02d}-{uuid.uuid4().hex[:8]}"
    sample = Sample(index=index, case=case, request_id=request_id)
    started = time.perf_counter()
    try:
        response = client.post(
            f"{GATEWAY_BASE}{API_PREFIX}/query/code",
            json={"message": case.message},
            headers={"X-Request-Id": request_id},
        )
    except httpx.TimeoutException:
        sample.rt_ms = (time.perf_counter() - started) * 1000
        sample.transport_error = "客户端超时"
        sample.blocking.append(("传输异常", "客户端等待网关超时"))
        return sample
    except httpx.HTTPError as exc:
        sample.rt_ms = (time.perf_counter() - started) * 1000
        sample.transport_error = f"{type(exc).__name__}: {exc}"
        sample.blocking.append(("传输异常", sample.transport_error))
        return sample

    sample.rt_ms = (time.perf_counter() - started) * 1000
    sample.status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        sample.blocking.append(("响应结构", "响应不是合法 JSON"))
        return sample
    if not isinstance(payload, dict):
        sample.blocking.append(("响应结构", "响应 JSON 顶层不是对象"))
        return sample
    sample.payload = payload

    echoed = payload.get("request_id")
    if echoed != request_id:
        sample.blocking.append(
            ("响应结构", f"request_id 回传不一致: 发送 {request_id}，返回 {echoed}")
        )

    if response.status_code != 200:
        kind, detail = classify_http_error(payload)
        sample.blocking.append((kind, detail))
        return sample

    evaluate(sample, payload, client, check_downloads)
    return sample


def preflight(client: httpx.Client) -> dict[str, Any] | None:
    """检查网关与数据目录，失败时给出明确修复指令。"""
    print("== 预检 ==")
    try:
        health = client.get(f"{GATEWAY_BASE}/actuator/health", timeout=5.0)
        print(f"网关健康: HTTP {health.status_code} {health.text[:120]}")
        if health.status_code != 200:
            raise httpx.HTTPError("网关未就绪")
    except httpx.HTTPError as exc:
        print(f"[失败] 无法访问 Java 网关 {GATEWAY_BASE}: {exc}")
        print("请先启动：./scripts/start-gateway.sh")
        return None

    try:
        tables = client.get(f"{GATEWAY_BASE}{API_PREFIX}/tables", timeout=10.0)
        catalog = tables.json() if tables.status_code == 200 else {}
    except httpx.HTTPError as exc:
        print(f"[失败] 无法读取数据表目录: {exc}")
        return None

    table_list = catalog.get("tables") or []
    print(f"数据表: {len(table_list)} 张，主表={catalog.get('primary_table')}")
    for table in table_list[:12]:
        print(f"  - {table.get('name')} rows={table.get('row_count')} role={table.get('role')}")
    for warning in catalog.get("warnings") or []:
        print(f"  [目录警告] {warning}")
    if not table_list:
        print("[失败] 数据表目录为空，请先导入 Excel 或运行 scripts/generate_mock_tables.py")
        return None
    print()
    return catalog


# ── 报告 ────────────────────────────────────────────────────────────────


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[position]


def _load_log_evidence(path: Path | None, request_ids: set[str]) -> dict[str, list[str]]:
    """从后端日志中按 request_id 提取失败证据（尽力而为）。"""
    if path is None or not path.exists() or not request_ids:
        return {}
    if path.stat().st_size > 50 * 1024 * 1024:
        return {}
    evidence: dict[str, list[str]] = {}
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                for request_id in request_ids:
                    if request_id in line:
                        evidence.setdefault(request_id, []).append(line.strip())
    except OSError:
        return {}
    return {key: value[-4:] for key, value in evidence.items()}


def _default_backend_log() -> Path | None:
    """优先使用 scripts/start-backend.sh 常见的重定向位置。"""
    candidates = (
        PROJECT_ROOT / ".runtime" / "logs" / "backend.log",
        PROJECT_ROOT / "backend" / ".runtime" / "logs" / "backend.log",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def print_report(
    samples: list[Sample],
    total_elapsed_s: float,
    catalog: dict[str, Any],
    output_path: Path | None,
) -> None:
    total = len(samples)
    passed = [sample for sample in samples if sample.passed]
    failed = [sample for sample in samples if not sample.passed]
    rts = [sample.rt_ms for sample in samples if sample.rt_ms > 0]

    print("=" * 78)
    print("E2E 压力测试报告")
    print("=" * 78)
    print(f"数据表数量: {len(catalog.get('tables') or [])}   总耗时: {total_elapsed_s:.1f}s")
    print(
        f"请求总数: {total}   通过: {len(passed)}   失败: {len(failed)}   "
        f"通过率: {len(passed) / total * 100:.1f}%"
    )
    if rts:
        print(
            "RT: 平均 {avg:.0f}ms  中位 {median:.0f}ms  P95 {p95:.0f}ms  最大 {maxv:.0f}ms".format(
                avg=statistics.fmean(rts),
                median=statistics.median(rts),
                p95=_percentile(rts, 0.95),
                maxv=max(rts),
            )
        )

    # 分类统计
    print("\n-- 分类通过率 " + "-" * 60)
    categories: dict[str, list[Sample]] = {}
    for sample in samples:
        categories.setdefault(sample.case.category, []).append(sample)
    for category, items in categories.items():
        ok = sum(1 for item in items if item.passed)
        avg_rt = statistics.fmean([item.rt_ms for item in items])
        print(f"  {category:<6} {ok}/{len(items)} 通过  平均 {avg_rt:.0f}ms")

    # 自修复
    repaired = [sample for sample in samples if sample.attempts > 1 or sample.retry_errors]
    extra_attempts = sum(max(0, sample.attempts - 1) for sample in samples)
    print("\n-- AI 自修复 " + "-" * 63)
    print(f"  触发重试的请求: {len(repaired)}/{total}   额外调用次数: {extra_attempts}")
    for sample in repaired:
        first_error = sample.retry_errors[0] if sample.retry_errors else "无 retry_errors"
        print(
            f"  #{sample.index:02d} attempts={sample.attempts} "
            f"结果={'通过' if sample.passed else '失败'}  首次错误: {_truncate(first_error, 90)}"
        )
    reason_counts = Counter(sample.retry_errors[0] for sample in repaired if sample.retry_errors)
    if reason_counts:
        print("  重试原因分布:")
        for reason, count in reason_counts.most_common(5):
            print(f"    x{count:<3} {_truncate(reason, 90)}")

    # 图表要求
    chart_cases = [sample for sample in samples if sample.case.chart]
    chart_ok = [sample for sample in chart_cases if sample.chart_type == sample.case.chart]
    print("\n-- 图表要求 " + "-" * 63)
    print(f"  显式图表要求: {len(chart_ok)}/{len(chart_cases)} 满足")
    for sample in chart_cases:
        mark = "OK" if sample.chart_type == sample.case.chart else "FAIL"
        print(
            f"  [{mark}] #{sample.index:02d} 期望 {sample.case.chart} "
            f"实际 {sample.chart_type}  {sample.case.message}"
        )

    # 导出链路
    download_checked = [sample for sample in samples if sample.download_ok is not None]
    word_checked = [sample for sample in samples if sample.word_ok is not None]
    print("\n-- 导出链路 " + "-" * 63)
    print(
        f"  Excel URL 校验: {sum(1 for s in download_checked if s.download_ok)}/"
        f"{len(download_checked)} 可下载"
    )
    print(
        f"  Word URL 校验: {sum(1 for s in word_checked if s.word_ok)}/"
        f"{len(word_checked)} 可下载"
    )

    # 详细明细
    print("\n-- 请求明细 " + "-" * 63)
    print(f"{'#':<3}{'类别':<8}{'RT(ms)':>8}  {'状态':<5}{'行数':>5}  {'图表':<6}{'重试':<5}结果")
    for sample in samples:
        print(
            f"{sample.index:<3}{sample.case.category:<8}{sample.rt_ms:>8.0f}  "
            f"{sample.status if sample.status is not None else '-':<5}"
            f"{(sample.row_count if sample.row_count is not None else '-'):>5}  "
            f"{(sample.chart_type or '-'):<6}{sample.attempts:<5}"
            f"{'PASS' if sample.passed else 'FAIL'}"
        )

    # 失败归因
    print("\n-- 失败归因 " + "-" * 63)
    if not failed:
        print("  全部通过")
    else:
        attributions: dict[str, list[Sample]] = {}
        for sample in failed:
            attributions.setdefault(sample.attribution, []).append(sample)
        for kind, items in attributions.items():
            print(f"  {kind}: {len(items)} 个（#{', #'.join(str(s.index) for s in items)}）")
        for sample in failed:
            print(f"\n  #{sample.index:02d} [{sample.case.category}] {sample.case.message}")
            print(
                f"     HTTP={sample.status} RT={sample.rt_ms:.0f}ms "
                f"attempts={sample.attempts} 归因={sample.attribution}"
            )
            for kind, detail in sample.blocking:
                print(f"     [{kind}] {_truncate(detail, 220)}")
            if sample.transport_error:
                print(f"     传输: {sample.transport_error}")
            if sample.retry_errors:
                print(f"     重试错误: {_truncate(' | '.join(sample.retry_errors), 240)}")
            if sample.warnings:
                print(f"     后端警告: {_truncate(' | '.join(sample.warnings), 240)}")
            if sample.generated_code:
                print(f"     生成代码: {_truncate(sample.generated_code, 240)}")
            if sample.log_evidence:
                print(f"     日志证据: {_truncate(' | '.join(sample.log_evidence), 320)}")

    # 警告聚合
    warning_counts: dict[str, int] = {}
    for sample in samples:
        for warning in sample.warnings:
            warning_counts[warning] = warning_counts.get(warning, 0) + 1
    if warning_counts:
        print("\n-- 后端警告 Top " + "-" * 60)
        for warning, count in sorted(
            warning_counts.items(), key=lambda item: item[1], reverse=True
        )[:8]:
            print(f"  x{count:<3} {_truncate(warning, 100)}")

    # 最慢
    print("\n-- 最慢请求 Top 5 " + "-" * 57)
    for sample in sorted(samples, key=lambda item: item.rt_ms, reverse=True)[:5]:
        print(
            f"  #{sample.index:02d} {sample.rt_ms:>7.0f}ms  "
            f"{'PASS' if sample.passed else 'FAIL'}  {sample.case.message}"
        )

    if output_path is not None:
        print(f"\n原始结果已保存: {output_path}")


def build_report_payload(
    samples: list[Sample],
    started_at: datetime,
    elapsed_s: float,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    return {
        "started_at": started_at.isoformat(),
        "elapsed_seconds": round(elapsed_s, 3),
        "catalog": {
            "primary_table": catalog.get("primary_table"),
            "table_count": len(catalog.get("tables") or []),
            "warnings": catalog.get("warnings") or [],
        },
        "summary": {
            "total": len(samples),
            "passed": sum(1 for sample in samples if sample.passed),
            "failed": sum(1 for sample in samples if not sample.passed),
            "avg_rt_ms": round(statistics.fmean([s.rt_ms for s in samples]), 1) if samples else 0,
        },
        "samples": [
            {
                "index": sample.index,
                "category": sample.case.category,
                "message": sample.case.message,
                "request_id": sample.request_id,
                "status": sample.status,
                "rt_ms": round(sample.rt_ms, 1),
                "passed": sample.passed,
                "attribution": sample.attribution,
                "blocking": [{"kind": kind, "detail": detail} for kind, detail in sample.blocking],
                "notes": sample.notes,
                "attempts": sample.attempts,
                "retry_errors": sample.retry_errors,
                "warnings": sample.warnings,
                "chart_type": sample.chart_type,
                "row_count": sample.row_count,
                "download_ok": sample.download_ok,
                "word_ok": sample.word_ok,
                "generated_code": sample.generated_code,
                "response": sample.payload,
            }
            for sample in samples
        ],
    }


# ── 入口 ────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 物管助手的 E2E 压测脚本")
    parser.add_argument("--timeout", type=float, default=120.0, help="单个请求超时秒数")
    parser.add_argument(
        "--no-download-check", action="store_true", help="不下载 download_url 校验文件"
    )
    parser.add_argument("--only", default="", help="只跑指定序号，例如 6,7,14")
    parser.add_argument("--output", type=Path, default=None, help="原始结果 JSON 路径")
    parser.add_argument(
        "--backend-log",
        type=Path,
        default=None,
        help="后端日志路径，失败时按 request_id 提取证据",
    )
    parser.add_argument("--strict", action="store_true", help="存在失败时以退出码 1 结束")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    selected = CASES
    if args.only.strip():
        wanted = {int(item) for item in args.only.split(",") if item.strip()}
        selected = tuple(case for index, case in enumerate(CASES, start=1) if index in wanted)
        if not selected:
            print(f"--only 未匹配任何用例: {args.only}")
            return 2

    started_at = datetime.now()
    started = time.perf_counter()
    with httpx.Client(timeout=args.timeout, trust_env=False) as client:
        catalog = preflight(client)
        if catalog is None:
            return 2

        samples: list[Sample] = []
        for index, case in enumerate(CASES, start=1):
            if case not in selected:
                continue
            print(f"[{index:02d}/{len(CASES)}] {case.category} | {case.message}")
            sample = run_case(index, case, client, run_id, not args.no_download_check)
            samples.append(sample)
            status = sample.status if sample.status is not None else "-"
            verdict = "PASS" if sample.passed else "FAIL"
            print(
                f"         -> {verdict} HTTP={status} RT={sample.rt_ms:.0f}ms "
                f"rows={sample.row_count} chart={sample.chart_type} "
                f"attempts={sample.attempts} issues={len(sample.blocking)}"
            )

    elapsed_s = time.perf_counter() - started

    # 失败请求的日志证据（若日志可用）
    backend_log = args.backend_log or _default_backend_log()
    if backend_log is not None:
        failed_ids = {sample.request_id for sample in samples if not sample.passed}
        evidence = _load_log_evidence(backend_log, failed_ids)
        for sample in samples:
            sample.log_evidence = evidence.get(sample.request_id, [])

    output_path = args.output
    if output_path is None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = DEFAULT_OUTPUT_DIR / f"e2e_{run_id}.json"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = build_report_payload(samples, started_at, elapsed_s, catalog)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print()
    print_report(samples, elapsed_s, catalog, output_path)

    has_failure = any(not sample.passed for sample in samples)
    return 1 if has_failure and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
