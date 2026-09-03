#!/bin/bash
# 把 build_standalone.sh 的产出打成部署 tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 0.1.0)}"
DIST_DIR="dist/standalone"
PKG="dist/pdf-distribute-${VERSION}.tar.gz"

[[ -d "$DIST_DIR" ]] || { echo "[ERROR] 不存在 $DIST_DIR，请先跑 scripts/build_standalone.sh"; exit 2; }
mkdir -p dist
tar -czf "$PKG" -C "$DIST_DIR" .
echo "[package] -> $PKG  ($(du -h "$PKG" | cut -f1))"
