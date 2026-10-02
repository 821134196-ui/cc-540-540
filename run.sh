#!/usr/bin/env bash
# 一条本地命令启动家长会预约系统（自动初始化种子，含教师请假样例）。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"

# 首次启动自动建库；加 --reset 可重建：./run.sh --reset
RESET=""
if [ "${2:-}" = "--reset" ]; then RESET="--reset"; fi

python3 -m app --host 127.0.0.1 --port "$PORT" $RESET
