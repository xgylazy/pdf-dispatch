#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 把预打包好的 pdf-distribute 部署包 SSH 分发到 N 台机器并启动。
# 绿色包=自带 Python 解释器 + venv 依赖 + 离线模型 + 启动脚本；目标机零预装零网络。
#
#  servers.txt 格式：
#    user@ip               worker（自动检测 CPU 核数作为并发）
#    user@ip app=scheduler  调度中心
#
# 用法： ./deploy.sh servers.txt
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 0.1.0)}"
PKG="dist/pdf-distribute-${VERSION}.tar.gz"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/pdf-distribute}"

[[ -f "$PKG" ]] || { echo "[ERROR] 不存在 $PKG；请先 scripts/build_standalone.sh && scripts/package.sh"; exit 2; }
[[ -f "${1:-}" ]] || { echo "[ERROR] 找不到 servers.txt: $1"; exit 2; }

SSH_OPTS="${SSH_OPTS:--i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=10}"
mapfile -t TARGETS < <(grep -vE '^\s*#|^\s*$' "$1")

echo "[deploy] pkg=$(basename "$PKG")  targets=${#TARGETS[@]}"
for t in "${TARGETS[@]}"; do echo "  $t"; done; echo

for target in "${TARGETS[@]}"; do
  host="${target%% *}"; kv="${target#* }"
  app="worker"
  for kvpair in $kv; do
    case "$kvpair" in
      app=*)      app="${kvpair#app=}" ;;
    esac
  done
  echo "==> $host ($app)"

  # 1. 先停旧进程（PID 文件不存在则跳过，kill -0 再确认进程真在跑）
  ssh $SSH_OPTS "$host" bash <<STOPREMOTE
    set -euo pipefail
    DEPLOY_DIR="$DEPLOY_DIR"
    if [[ -f "\$DEPLOY_DIR/data/$app.pid" ]]; then
      _pid=\$(cat "\$DEPLOY_DIR/data/$app.pid")
      if kill -0 "\$_pid" 2>/dev/null; then
        echo "  [stop] pid=\$_pid"
        kill -TERM "\$_pid" 2>/dev/null || true
        for _i in 1 2 3 4 5; do kill -0 "\$_pid" 2>/dev/null || break; sleep 1; done
        kill -KILL "\$_pid" 2>/dev/null || true
      fi
      rm -f "\$DEPLOY_DIR/data/$app.pid"
    fi
STOPREMOTE

  # 2. 再解压覆盖（旧进程已停，不会读到不完整文件）
  scp $SSH_OPTS "$PKG" "$host:/tmp/$(basename "$PKG")"
  # 3. 最后启新进程
  ssh $SSH_OPTS "$host" bash <<REMOTE
    set -euo pipefail
    mkdir -p "$DEPLOY_DIR"
    tar -xzf "/tmp/$(basename "$PKG")" -C "$DEPLOY_DIR"
    cd "$DEPLOY_DIR"
    mkdir -p data logs
    SCHEDULER_URL="\${SCHEDULER_URL:-http://10.0.0.10:8000}" \\
    BACKEND_ID="\$(hostname)-$app" \\
      nohup "./start_$app.sh" > "data/logs/$app.log" 2>&1 &
    echo \$! > "data/$app.pid"
    echo "  [started] pid=\$(cat data/$app.pid) $app"
REMOTE
  echo "<== $host done"; echo
done
echo "[deploy] 全部完成"
