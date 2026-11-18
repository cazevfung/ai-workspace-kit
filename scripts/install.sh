#!/usr/bin/env bash
# AI 工作区套件 · macOS / Linux 安装入口
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "需要 Python 3.10 或更高版本，但没有找到 python3 / python。" >&2
  exit 2
fi

exec "$PY" install.py "$@"
