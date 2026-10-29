#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 -m http.server "${FRONTEND_PORT:-8081}" \
  --bind "${FRONTEND_HOST:-127.0.0.1}" \
  --directory "$PROJECT_ROOT/frontend"
