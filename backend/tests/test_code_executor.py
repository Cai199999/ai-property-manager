import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.code_executor import (
    CodeExecutor,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from app.services.code_safety import UnsafeCodeError

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (Path(sys.prefix) / "bin" / "python").exists(),
    reason="需要 bwrap 和当前虚拟环境解释器",
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "code_exec_timeout_seconds": 8.0,
        "code_max_result_rows": 100,
        "code_max_result_columns": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "项目名称": ["A", "A", "B"],
            "工单编号": ["T-1", "T-2", "T-3"],
            "状态": ["已完成", "处理中", "已完成"],
            "费用金额": [10.5, 20.0, 5.0],
            "受理时间": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]),
        }
    )


def test_executor_runs_aggregation_in_sandbox() -> None:
    executor = CodeExecutor(_settings())
    result = executor.execute(
        _frame(),
        """
result_df = (
    source_df.groupby("项目名称", as_index=False)
    .agg(工单量=("工单编号", "size"), 总费用=("费用金额", "sum"))
    .sort_values("工单量", ascending=False)
)
""",
        timeout=8,
    )

    assert result.row_count == 2
    assert result.rows[0]["项目名称"] == "A"
    assert result.rows[0]["工单量"] == 2
    assert result.rows[0]["总费用"] == 30.5
    assert result.columns[1]["dtype"] == "int64"


def test_executor_merges_multiple_tables_in_sandbox() -> None:
    orders = pd.DataFrame(
        {
            "单号": ["T-1", "T-2", "T-3"],
            "项目编号": ["P1", "P1", "P2"],
            "费用金额": [10.0, 20.0, 5.0],
        }
    )
    projects = pd.DataFrame(
        {
            "项目编号": ["P1", "P2"],
            "所属区域": ["朝阳区", "海淀区"],
        }
    )
    result = CodeExecutor(_settings()).execute(
        orders,
        """
merged = tables["工单表"].merge(
    tables["项目表"],
    on="项目编号",
    how="left",
    validate="many_to_one",
)
result_df = (
    merged.groupby("所属区域", as_index=False)
    .agg(工单量=("单号", "size"), 总费用=("费用金额", "sum"))
    .sort_values("工单量", ascending=False)
)
""",
        timeout=8,
        tables={"工单表": orders, "项目表": projects},
        primary_table="工单表",
    )

    assert result.row_count == 2
    assert result.rows[0]["所属区域"] == "朝阳区"
    assert result.rows[0]["工单量"] == 2
    assert result.rows[0]["总费用"] == 30.0


def test_executor_rejects_file_write_before_process_starts() -> None:
    with pytest.raises(UnsafeCodeError):
        CodeExecutor(_settings()).execute(
            _frame(),
            'result_df = source_df.to_csv("/tmp/escape.csv")',
            timeout=8,
        )


def test_executor_reports_runtime_error_with_generated_line() -> None:
    with pytest.raises(SandboxExecutionError) as error:
        CodeExecutor(_settings()).execute(
            _frame(),
            'result_df = source_df["不存在的列"]',
            timeout=8,
        )

    message = str(error.value)
    assert "KeyError" in message
    assert "第 1 行" in message


def test_executor_rejects_non_dataframe_result() -> None:
    with pytest.raises(SandboxExecutionError) as error:
        CodeExecutor(_settings()).execute(
            _frame(),
            "result_df = 1",
            timeout=8,
        )

    assert "DataFrame" in str(error.value)


def test_executor_enforces_result_row_limit() -> None:
    with pytest.raises(SandboxExecutionError) as error:
        CodeExecutor(_settings(code_max_result_rows=1)).execute(
            _frame(),
            "result_df = source_df.copy()",
            timeout=8,
        )

    assert "行数" in str(error.value)


def test_executor_kills_infinite_loop() -> None:
    with pytest.raises(SandboxTimeoutError):
        CodeExecutor(_settings()).execute(
            _frame(),
            "result_df = source_df.copy()\nwhile True:\n    pass",
            timeout=1,
        )
