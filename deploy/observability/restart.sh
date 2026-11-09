#!/usr/bin/env bash
# 重启 Agent Forge 可观测性后端（trace 与指标均已落盘，重启不丢）
# 用法：bash deploy/observability/restart.sh
set -euo pipefail
cd "$(dirname "$0")"

docker compose restart

# 等待就绪
for i in $(seq 1 30); do
  if curl -sf -o /dev/null http://127.0.0.1:16686/api/services \
     && curl -sf -o /dev/null http://127.0.0.1:9090/-/ready; then
    echo "可观测性后端已就绪（Jaeger :16686 / Prometheus :9090）"
    exit 0
  fi
  sleep 2
done
echo "警告：后端未在 60 秒内就绪，请查看 docker compose logs" >&2
exit 1
