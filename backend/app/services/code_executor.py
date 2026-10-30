"""在隔离子进程中执行生成代码。

Linux 优先使用 bubblewrap（bwrap）做命名空间隔离；Windows 绿色版没有
bwrap，改用受限参数的**独立子进程**执行（清空环境变量、独立临时工作目录、
固定解释器、超时强杀）。无论哪种模式，代码都只在子进程里运行，绝不在
FastAPI 进程内执行。

未启用 bwrap 时，安全边界主要由 ``code_safety.validate_generated_code``
的 AST 校验承担：禁止危险导入、文件写入、网络与反射类调用。该边界弱于
命名空间隔离，因此仅适用于可信的本地绿色版部署。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from app.config import Settings
from app.services.code_safety import (
    UnsafeCodeError,
    sanitize_error_text,
    validate_generated_code,
)

logger = logging.getLogger(__name__)


class SandboxUnavailableError(RuntimeError):
    """本机没有可用的隔离执行环境。"""


class SandboxTimeoutError(RuntimeError):
    """生成代码在沙箱中执行超时。"""


class SandboxExecutionError(RuntimeError):
    """生成代码执行失败，消息可安全反馈给模型。"""


@dataclass(frozen=True, slots=True)
class CodeExecutionResult:
    """沙箱返回的通用结果表。"""

    columns: list[dict[str, str]]
    rows: list[dict[str, Any]]
    row_count: int
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


class CodeExecutor:
    """管理沙箱生命周期和父子进程协议。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._windows = sys.platform == "win32"
        # Windows 上 bwrap 不可能存在，直接跳过探测；也避免 which() 被
        # 某些安全软件拦截时产生额外开销或误判。
        self._bwrap = None if self._windows else shutil.which("bwrap")
        self._frozen = bool(getattr(sys, "frozen", False))
        self._venv_root = Path(sys.prefix).resolve()
        if self._windows:
            # Windows 下始终使用当前解释器：全局安装、conda、嵌入式 Python
            # 的 Scripts 目录不一定包含 python.exe，按 sys.prefix 猜测会把
            # 正常环境误判为“沙箱不可用”（线上 503 的根因）。
            self._python = Path(sys.executable)
        else:
            python_candidates = (
                self._venv_root / "bin" / "python",
                self._venv_root / "bin" / "python3",
            )
            self._python = next(
                (candidate for candidate in python_candidates if candidate.exists()),
                python_candidates[0],
            )
        self._runner = Path(__file__).resolve().parents[1] / "sandbox" / "runner.py"
        self._mode = self._resolve_mode()
        if self._mode == "subprocess":
            reason = "Windows 平台无 bwrap" if self._windows else "未找到 bwrap"
            logger.warning("%s，代码执行降级为受限子进程模式（仅依赖 AST 安全校验）", reason)

    def _resolve_mode(self) -> str:
        """决定执行方式：bwrap 隔离 / 受限子进程 / 不可用。

        - ``CODE_SANDBOX_MODE=disabled``：显式关闭，不执行任何生成代码。
        - Windows：跳过 bwrap 探测，直接使用当前解释器的子进程模式。
        - 其他平台：找到 bwrap 用命名空间隔离，找不到则降级为子进程。
        """
        if self._settings.code_sandbox_mode != "bwrap":
            return "unavailable"
        if not self._interpreter_ready():
            return "unavailable"
        if self._windows:
            return "subprocess"
        if self._bwrap:
            return "bwrap"
        return "subprocess"

    def _interpreter_ready(self) -> bool:
        """解释器与 runner 是否可用；打包成 exe 后两者由 PyInstaller 内置。"""
        if self._frozen:
            return True
        if self._windows:
            # 当前进程能运行，说明 sys.executable 一定有效。
            return bool(sys.executable)
        return self._python.exists() and self._runner.exists()

    @property
    def available(self) -> bool:
        return self._mode != "unavailable"

    @property
    def mode(self) -> str:
        """当前执行模式，便于健康检查与日志排查。"""
        return self._mode

    def execute(
        self,
        source_df: pd.DataFrame,
        code: str,
        *,
        timeout: float | None = None,
        tables: Mapping[str, pd.DataFrame] | None = None,
        primary_table: str | None = None,
    ) -> CodeExecutionResult:
        """执行代码并返回 JSON 安全的结果表。

        ``tables`` 提供多表数据；未提供时沿用旧的单表 ``source_df`` 协议。
        """
        if self._mode == "unavailable":
            raise SandboxUnavailableError("未找到 bwrap 或虚拟环境解释器，拒绝执行生成代码")

        # 执行器再校验一次，避免调用方绕过 CodeGenerator 直接调用 execute。
        validate_generated_code(code, max_chars=self._settings.code_max_code_chars)

        frames = _resolve_frames(source_df, tables, primary_table)
        payload_frames = {name: _serialize_frame(frame) for name, frame in frames.items()}
        payload = {
            "code": code,
            "frames": payload_frames,
            "primary_table": primary_table or next(iter(frames)),
            "limits": {
                "max_rows": self._settings.code_max_result_rows,
                "max_columns": self._settings.code_max_result_columns,
                "max_cell_chars": self._settings.code_max_cell_chars,
                "max_output_bytes": 5_000_000,
                "max_tables": self._settings.table_max_files,
                "max_frame_rows": self._settings.table_max_rows,
                "max_frame_columns": self._settings.table_max_columns,
            },
        }
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload_bytes) > self._settings.code_max_payload_bytes:
            raise SandboxExecutionError("输入数据过大，无法送入代码执行沙箱")

        started_at = time.perf_counter()
        result = self._run(
            payload_bytes,
            timeout=timeout or self._settings.code_exec_timeout_seconds,
        )
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        warnings = list(result.get("warnings", []))
        if self._mode == "subprocess":
            # 明确告知调用方：没有内核级隔离，只有进程边界 + AST 校验。
            warnings.append("当前未启用 bwrap 隔离，代码在受限子进程中执行")
        return CodeExecutionResult(
            columns=result["columns"],
            rows=result["rows"],
            row_count=result["row_count"],
            warnings=warnings,
            elapsed_ms=elapsed_ms,
        )

    def _run(self, payload: bytes, *, timeout: float) -> dict[str, Any]:
        logger.info(
            "启动代码沙箱 mode=%s platform=%s timeout=%.1fs",
            self._mode,
            sys.platform,
            timeout,
        )
        if self._mode == "bwrap":
            stdout, stderr, returncode = self._run_bwrap(payload, timeout=timeout)
        else:
            stdout, stderr, returncode = self._run_direct(payload, timeout=timeout)
        return self._parse_result(stdout, stderr, returncode)

    def _run_direct(self, payload: bytes, *, timeout: float) -> tuple[bytes, bytes, int]:
        """无 bwrap 时用 subprocess.run 执行，超时由 Python 侧强制终止。

        Windows 的默认路径：使用 ``sys.executable`` 启动独立子进程，
        代码不会进入 FastAPI 进程。
        """
        workdir = self._make_workdir()
        try:
            completed = subprocess.run(
                self._build_direct_command(),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=self._direct_environment(),
                cwd=workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(f"代码执行超过 {timeout:.1f} 秒") from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return completed.stdout, completed.stderr, completed.returncode

    def _run_bwrap(self, payload: bytes, *, timeout: float) -> tuple[bytes, bytes, int]:
        """bwrap 模式需要进程组强杀，因此保留 Popen。"""
        command = self._build_bwrap_command()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            process.communicate()
            raise SandboxTimeoutError(f"代码执行超过 {timeout:.1f} 秒") from exc
        return stdout, stderr, process.returncode

    def _parse_result(self, stdout: bytes, stderr: bytes, returncode: int) -> dict[str, Any]:
        """解析 runner 的 JSON 输出，异常信息统一脱敏后上抛。"""
        if returncode != 0 and not stdout.strip():
            detail = sanitize_error_text(stderr.decode("utf-8", errors="replace"))
            if self._mode == "bwrap" and ("bwrap:" in detail or "namespace" in detail.lower()):
                raise SandboxUnavailableError(f"沙箱启动失败: {detail}")
            raise SandboxExecutionError(f"沙箱进程异常退出: {detail or returncode}")

        try:
            payload_result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = sanitize_error_text(stderr.decode("utf-8", errors="replace"))
            raise SandboxExecutionError(f"沙箱返回格式异常: {detail or '非 JSON 响应'}") from exc

        if not isinstance(payload_result, dict):
            raise SandboxExecutionError("沙箱返回格式异常")

        if payload_result.get("ok") is not True:
            error_type = sanitize_error_text(payload_result.get("error_type"), limit=80)
            message = sanitize_error_text(payload_result.get("message"), limit=600)
            line = payload_result.get("line")
            line_text = f"（第 {line} 行）" if isinstance(line, int) and line > 0 else ""
            raise SandboxExecutionError(
                f"{error_type or '执行错误'}: {message or '未知错误'}{line_text}"
            )

        return _validate_result(payload_result, self._settings)

    def _build_direct_command(self) -> list[str]:
        """无 bwrap 时的子进程命令。

        - 源码运行：``python -I -B runner.py``，-I 隔离用户 site-packages
          与 PYTHONPATH，-B 不写字节码。
        - PyInstaller 打包后：同一 exe 以 ``--sandbox-runner`` 参数再次启动，
          直接进入 runner 逻辑。
        """
        if self._frozen:
            return [sys.executable, "--sandbox-runner"]
        if self._windows:
            # 始终用当前解释器，Source 版也可能是全局/conda 安装。
            return [sys.executable, "-I", "-B", str(self._runner)]
        return [str(self._python), "-I", "-B", str(self._runner)]

    @staticmethod
    def _direct_environment() -> dict[str, str]:
        """只保留启动解释器必需的系统变量，避免密钥等敏感信息泄漏给子进程。"""
        preserved = (
            "PATH",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LOCALAPPDATA",
        )
        environment = {key: os.environ[key] for key in preserved if key in os.environ}
        environment.setdefault("PATH", os.defpath)
        environment.update(
            PYTHONHASHSEED="0",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
        )
        return environment

    @staticmethod
    def _make_workdir() -> str:
        """独立临时工作目录：生成代码即便写文件也落在一次性目录里。"""
        return tempfile.mkdtemp(prefix="code-sandbox-")

    def _build_bwrap_command(self) -> list[str]:
        if not self._bwrap:
            raise SandboxUnavailableError("未找到 bwrap")

        command = [
            self._bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            str(self._venv_root),
            str(self._venv_root),
            "--ro-bind",
            str(self._runner),
            "/runner.py",
        ]

        # 所有 bwrap 选项都必须在最终可执行文件之前传入；/lib 和 /lib64
        # 在部分发行版是符号链接，存在才绑定。
        for path in ("/lib", "/lib64", "/etc/alternatives", "/etc/ld.so.cache"):
            if Path(path).exists():
                command.extend(["--ro-bind", path, path])

        command.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--chdir",
                "/tmp",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "PYTHONHASHSEED",
                "0",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                str(self._python),
                "-I",
                "-B",
                "/runner.py",
            ]
        )
        return command


def _resolve_frames(
    source_df: pd.DataFrame,
    tables: Mapping[str, pd.DataFrame] | None,
    primary_table: str | None,
) -> dict[str, pd.DataFrame]:
    """统一单表/多表输入，并校验表名和主表是否存在。"""
    if tables is None:
        name = primary_table or "source_df"
        return {name: source_df}

    frames = dict(tables)
    if not frames:
        raise SandboxExecutionError("没有可执行的数据表")
    if len(frames) > 50:
        raise SandboxExecutionError("数据表数量过多，无法送入代码执行沙箱")

    for name, frame in frames.items():
        if not isinstance(name, str) or not name.strip():
            raise SandboxExecutionError("数据表名不合法")
        if len(name) > 80:
            raise SandboxExecutionError("数据表名过长")
        if not isinstance(frame, pd.DataFrame):
            raise SandboxExecutionError(f"数据表 {name} 不是 DataFrame")

    resolved_primary = primary_table or next(iter(frames))
    if resolved_primary not in frames:
        raise SandboxExecutionError("主表不在 tables 中")
    return frames


def _serialize_frame(frame: pd.DataFrame) -> dict[str, Any]:
    """使用 JSON 传输数据，避免 pickle 反序列化执行任意代码。"""
    columns = [str(column) for column in frame.columns]
    if len(set(columns)) != len(columns):
        raise SandboxExecutionError("source_df 存在重复列名，无法安全传输")

    try:
        values = json.loads(
            frame.to_json(
                orient="values",
                date_format="iso",
                force_ascii=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise SandboxExecutionError("source_df 无法序列化为沙箱输入") from exc

    return {
        "columns": columns,
        "dtypes": [str(frame.iloc[:, index].dtype) for index in range(len(columns))],
        "values": values,
    }


def _validate_result(result: dict[str, Any], settings: Settings) -> dict[str, Any]:
    columns = result.get("columns")
    rows = result.get("rows")
    row_count = result.get("row_count")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise SandboxExecutionError("沙箱结果缺少 columns 或 rows")
    if not isinstance(row_count, int) or row_count != len(rows):
        raise SandboxExecutionError("沙箱结果 row_count 不一致")
    if row_count > settings.code_max_result_rows:
        raise SandboxExecutionError("沙箱结果行数超过上限")
    if len(columns) > settings.code_max_result_columns:
        raise SandboxExecutionError("沙箱结果列数超过上限")

    names: list[str] = []
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("name"), str):
            raise SandboxExecutionError("沙箱结果列定义不合法")
        names.append(column["name"])
    if len(set(names)) != len(names):
        raise SandboxExecutionError("沙箱结果存在重复列名")

    for row in rows:
        if not isinstance(row, dict) or set(row) != set(names):
            raise SandboxExecutionError("沙箱结果行结构与列定义不一致")

    warnings = result.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        warnings = []
    return {
        "columns": [
            {"name": name, "dtype": str(columns[index].get("dtype", ""))}
            for index, name in enumerate(names)
        ],
        "rows": rows,
        "row_count": row_count,
        "warnings": warnings,
    }


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """杀掉沙箱子进程，避免超时后继续占用资源。

    POSIX 借助独立进程组整组强杀；Windows 没有 killpg，直接终止该进程。
    """
    if os.name == "nt":
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
