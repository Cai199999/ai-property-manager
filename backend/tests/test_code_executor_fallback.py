"""无 bwrap 环境（Windows 绿色版）下的降级执行测试。"""

from __future__ import annotations

import sys

import pandas as pd
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.code_executor import CodeExecutor, SandboxUnavailableError


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "deepseek_api_key": SecretStr(""),
        "code_exec_timeout_seconds": 20.0,
        "code_max_result_rows": 100,
        "code_max_result_columns": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_falls_back_to_subprocess_when_bwrap_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """找不到 bwrap 时应降级为受限子进程，而不是直接不可用。"""
    monkeypatch.setattr("app.services.code_executor.shutil.which", lambda _name: None)
    executor = CodeExecutor(_settings())

    assert executor.available is True
    assert executor.mode == "subprocess"

    source = pd.DataFrame({"金额": [1.0, 2.0, 3.0]})
    result = executor.execute(
        source,
        "result_df = pd.DataFrame({'合计': [float(source_df['金额'].sum())]})",
    )

    assert result.rows == [{"合计": 6.0}]
    assert result.row_count == 1
    assert any("bwrap" in warning for warning in result.warnings)


def test_windows_skips_bwrap_probe_and_uses_current_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 不应探测 bwrap，也不应因 sys.prefix 布局不同而被判为不可用。"""

    def _fail_probe(_name: str) -> str:
        pytest.fail("Windows 平台不应调用 shutil.which('bwrap')")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("app.services.code_executor.shutil.which", _fail_probe)

    executor = CodeExecutor(_settings())

    assert executor.mode == "subprocess"
    assert executor.available is True
    command = executor._build_direct_command()
    assert command[0] == sys.executable
    assert command[-1].endswith("runner.py")


def test_disabled_mode_stays_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 disabled 时必须仍然拒绝执行。"""
    monkeypatch.setattr("app.services.code_executor.shutil.which", lambda _name: None)
    executor = CodeExecutor(_settings(code_sandbox_mode="disabled"))

    assert executor.mode == "unavailable"
    assert executor.available is False
    with pytest.raises(SandboxUnavailableError):
        executor.execute(pd.DataFrame({"a": [1]}), "result_df = source_df")
