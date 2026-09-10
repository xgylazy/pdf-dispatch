#!/usr/bin/env bash
# 并行部署入口（实际逻辑在 deploy.py，本文件仅为兼容保留的薄封装）
# 用法不变：./scripts/deploy.sh servers.txt
set -euo pipefail
cd "$(dirname "$0")/.."
exec python scripts/deploy.py "$@"
