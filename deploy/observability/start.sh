#!/usr/bin/env bash
# Agent Forge 可观测性后端一键启动（Jaeger traces + Prometheus metrics）
# 用法：bash deploy/observability/start.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] 启动容器（Jaeger + Prometheus）..."
docker compose up -d

# 修正 badger 数据卷属主（Jaeger 以 uid 10001 运行，新建命名卷默认 root
# 属主会导致启动失败；幂等，已正确时无副作用）
docker run --rm -v agentforge-observability_jaeger-badger:/badger --user root \
  --entrypoint chown docker.1ms.run/jaegertracing/all-in-one:latest \
  -R 10001:10001 /badger >/dev/null 2>&1 || true

echo "[2/3] 等待就绪..."
for i in $(seq 1 30); do
  jaeger_ok=0; prom_ok=0
  curl -sf -o /dev/null http://127.0.0.1:16686/api/services && jaeger_ok=1
  curl -sf -o /dev/null http://127.0.0.1:9090/-/ready && prom_ok=1
  [ "$jaeger_ok" = 1 ] && [ "$prom_ok" = 1 ] && break
  sleep 2
done
if [ "$jaeger_ok" != 1 ] || [ "$prom_ok" != 1 ]; then
  echo "警告：后端未在 60 秒内就绪，请查看 docker compose logs" >&2
  exit 1
fi

echo "[3/3] 检查 agentforge 接入配置..."
if grep -q '^AGENTFORGE_OTLP_TRACES_ENDPOINT=' ../../.env 2>/dev/null; then
  echo ".env 已配置 OTLP 端点"
else
  echo "提示：在 agentforge/.env 中添加以下配置并重启服务（systemctl restart agentforge-svc）"
  echo '  AGENTFORGE_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces'
  echo '  AGENTFORGE_OTLP_METRICS_ENDPOINT=http://127.0.0.1:9090/api/v1/otlp/v1/metrics'
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "就绪："
echo "  Jaeger UI（链路追踪）:  http://${IP:-<服务器IP>}:16686"
echo "  Prometheus（指标查询）: http://${IP:-<服务器IP>}:9090"
