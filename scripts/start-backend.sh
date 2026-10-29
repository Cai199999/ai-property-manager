#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT/backend"

exec "$PROJECT_ROOT/.venv/bin/uvicorn" app.main:app \
  --host "${BACKEND_HOST:-0.0.0.0}" \
  --port "${BACKEND_PORT:-8000}" \
  --reload
