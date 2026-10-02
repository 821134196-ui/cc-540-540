#!/usr/bin/env bash
# 一条本地命令：初始化（或重建）演示数据并启动服务，自带教师请假样例。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# 数据不存在则自动种子；需要完全重建可执行：./run.sh --reset
if [[ "${1:-}" == "--reset" ]]; then
  python3 -m app.seed --reset
elif [[ ! -f data/conference.db ]]; then
  python3 -m app.seed
fi

echo "----------------------------------------"
echo "打开浏览器访问： http://127.0.0.1:${PORT}"
echo "演示账号见启动输出，所有账号密码：123456"
echo "----------------------------------------"
exec python3 -m app.server --port "${PORT}"
