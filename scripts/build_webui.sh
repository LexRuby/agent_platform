#!/usr/bin/env bash
# 重建 Web UI 构建产物 webui/（源码已入库 webui-src/，无需外部 clone）。
# 用法：bash scripts/build_webui.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/webui-src"
OUT="$ROOT/webui"

command -v node >/dev/null || { echo "错误：未找到 node（需要 node >= 20）" >&2; exit 1; }
[ -d "$SRC" ] || { echo "错误：前端源码目录不存在 $SRC" >&2; exit 1; }

cd "$SRC"
if [ ! -d node_modules ]; then
    npm install
fi
npm run build

rm -rf "$OUT" && mkdir "$OUT"
cp -r dist/* "$OUT/"
echo "已重建 $OUT"
