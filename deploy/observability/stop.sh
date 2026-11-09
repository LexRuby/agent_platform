#!/usr/bin/env bash
# 停止 Agent Forge 可观测性后端（数据保留在容器卷/内存中，stop 不删除）
# 彻底清理：bash stop.sh --purge（删除容器；Prometheus 历史数据一并丢弃）
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--purge" ]; then
  docker compose down -v
  echo "已停止并删除容器"
else
  docker compose stop
  echo "已停止（restart 策略下次开机不会自启；重新启动用 start.sh）"
fi
