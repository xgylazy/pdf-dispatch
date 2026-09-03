#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
# CI 会 export VERSION；如果未设置则从 VERSION 文件读，最后 fallback 0.1.0
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 0.1.0)}"

echo "[packaging] VERSION=$VERSION"
tar -czf "dist/pdf-distribute-worker-${VERSION}.tar.gz" -C "dist/standalone-worker" .
tar -czf "dist/pdf-distribute-scheduler-${VERSION}.tar.gz" -C "dist/standalone-scheduler" .
sha256sum dist/pdf-distribute-*.tar.gz > dist/SHA256SUMS

echo "[done]"
echo "  worker:    dist/pdf-distribute-worker-${VERSION}.tar.gz  ($(du -h "dist/pdf-distribute-worker-${VERSION}.tar.gz" | cut -f1))"
echo "  scheduler: dist/pdf-distribute-scheduler-${VERSION}.tar.gz  ($(du -h "dist/pdf-distribute-scheduler-${VERSION}.tar.gz" | cut -f1))"
