"""后端统一启动入口（源码运行与 PyInstaller 打包共用）。

用法：
    python serve.py                     # 启动 FastAPI（默认 127.0.0.1:8000）
    python serve.py --sandbox-runner    # 内部协议：作为代码执行子进程运行

``--sandbox-runner`` 供 ``CodeExecutor`` 在 Windows（无 bwrap）时调用：
PyInstaller 打包后 ``sys.executable`` 是应用自身，因此需要用同一个 exe
加参数的方式进入沙箱 runner，而不能再执行一个独立的 python 解释器。
"""

from __future__ import annotations

import os
import sys


def _run_sandbox_runner() -> int:
    """进入沙箱子进程协议：stdin 读 JSON，stdout 写 JSON。"""
    from app.sandbox.runner import main as runner_main

    return runner_main()


def _run_server() -> int:
    import uvicorn

    from app.main import create_app

    host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def main() -> int:
    if "--sandbox-runner" in sys.argv[1:]:
        return _run_sandbox_runner()
    return _run_server()


if __name__ == "__main__":
    raise SystemExit(main())
