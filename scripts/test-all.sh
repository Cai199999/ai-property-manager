#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Python 测试"
cd "$PROJECT_ROOT/backend"
"$PROJECT_ROOT/.venv/bin/pytest"

echo "==> Java 网关测试"
cd "$PROJECT_ROOT/gateway"
mvn test
