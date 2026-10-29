#!/usr/bin/env python3
"""生成多表联查用的 3 张 Mock Excel 与 manifest.json。

用法：
    ./scripts/generate_mock_tables.py
    ./scripts/generate_mock_tables.py --output backend/.runtime/rpa_downloads/tables
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.mock_tables import generate_mock_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成物业多表 Mock Excel")
    parser.add_argument(
        "--output",
        type=Path,
        default=BACKEND_DIR / ".runtime" / "rpa_downloads" / "tables",
        help="输出目录，默认 backend/.runtime/rpa_downloads/tables",
    )
    args = parser.parse_args()

    summary = generate_mock_tables(args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n已生成 3 个同构工单表：表头完全一致，分析时使用 pd.concat 纵向合并。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
