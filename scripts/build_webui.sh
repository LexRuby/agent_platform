#!/usr/bin/env bash
# 重建 Web UI 构建产物 webui/（源码已入库 webui-src/，无需外部 clone）。
# 用法：bash scripts/build_webui.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/webui-src"
OUT="$ROOT/webui"

command -v node >/dev/null || { echo "错误：未找到 node（需要 node >= 20）" >&2; exit 1; }
# node >= 20 才支持构建链的语法（v12 会 "Unexpected token '?'"）；
# PATH 里可能排着老 node（如 /usr/bin/node），优先探测可用的新版本
NODE_MAJOR="$(node --version 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')"
if [ "${NODE_MAJOR:-0}" -lt 20 ] && [ -x /usr/local/bin/node ]; then
    export PATH="/usr/local/bin:$PATH"
    NODE_MAJOR="$(node --version | sed 's/^v\([0-9]*\).*/\1/')"
fi
[ "${NODE_MAJOR:-0}" -ge 20 ] || { echo "错误：node 版本过低（需要 >= 20，当前 $(node --version 2>/dev/null || echo 未安装)）" >&2; exit 1; }
[ -d "$SRC" ] || { echo "错误：前端源码目录不存在 $SRC" >&2; exit 1; }

cd "$SRC"
if [ ! -d node_modules ]; then
    npm install
fi
npm run build

# 增量部署（禁止整目录删除！）：
# - index.html 直接被新文件覆盖（响应带 no-cache，刷新即新版）
# - assets 按内容 hash 命名，新旧文件名不冲突，旧文件保留——
#   部署前已在浏览器里打开的旧页面仍能懒加载到旧 chunk，
#   否则会 "Failed to fetch dynamically imported module" 页面崩溃
#   （2026-09-04 用户点击技能详情报错，另有前端自愈刷新兜底）
# - 需要彻底清理历史 assets 时手动删除整个 webui/ 目录后重跑本脚本
mkdir -p "$OUT"
cp -r dist/* "$OUT/"
echo "已更新 $OUT（assets 增量保留，旧 chunk 不删除）"
