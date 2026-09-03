#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 0.1.0)}"
PKG_WK="dist/pdf-distribute-worker-${VERSION}.tar.gz"
PKG_SC="dist/pdf-distribute-scheduler-${VERSION}.tar.gz"
DIR_WK="/opt/pdf-worker"
DIR_SC="/opt/pdf-scheduler"

[[ -f "$PKG_WK" ]] || { echo "[ERROR] 不存在 $PKG_WK"; exit 2; }
[[ -f "$PKG_SC" ]] || { echo "[ERROR] 不存在 $PKG_SC"; exit 2; }
[[ -f "${1:-}" ]] || { echo "[ERROR] 找不到 servers.txt: $1"; exit 2; }

SSH_OPTS="${SSH_OPTS:--i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=10}"
mapfile -t TARGETS < <(grep -vE '^\s*#|^\s*$' "$1")

# 从 servers.txt 里找 scheduler IP，worker 自动连这个 IP 的 8000 端口
SCHEDULER_IP=""
for t in "${TARGETS[@]}"; do
  host="${t%% *}"; rest="${t#* }"
  for kv in $rest; do
    [[ "$kv" == "app=scheduler" ]] && SCHEDULER_IP="${host#*@}" && break 2
  done
done
[[ -n "$SCHEDULER_IP" ]] || { echo "[ERROR] servers.txt 里没找到 app=scheduler 的行"; exit 2; }
export SCHEDULER_URL="http://${SCHEDULER_IP}:8000"
echo "[deploy] scheduler=$SCHEDULER_URL  targets=${#TARGETS[@]}"
for t in "${TARGETS[@]}"; do echo "  $t"; done; echo

for target in "${TARGETS[@]}"; do
  host="${target%% *}"; rest="${target#* }"
  app="worker"
  for kv in $rest; do
    case "$kv" in app=*) app="${kv#app=}" ;; esac
  done
  remote_host="${host#*@}"

  if [[ "$app" == "scheduler" ]]; then
    PKG="$PKG_SC"; DIR="$DIR_SC"
  else
    PKG="$PKG_WK"; DIR="$DIR_WK"
  fi
  echo "==> $remote_host ($app -> $DIR, scheduler=$SCHEDULER_URL)"

  scp $SSH_OPTS "$PKG" "$remote_host:/tmp/$(basename "$PKG")"
  # 对 worker 注入 SCHEDULER_URL；scheduler 不需要传
  REMOTE_ENV=""
  [[ "$app" == "worker" ]] && REMOTE_ENV="SCHEDULER_URL=$SCHEDULER_URL"

  ssh $SSH_OPTS "$remote_host" bash <<REMOTE
    set -uo pipefail
    mkdir -p "$DIR"
    tar -xzf "/tmp/$(basename "$PKG")" -C "$DIR"
    cd "$DIR"
    $REMOTE_ENV nohup ./start.sh > "$DIR/data/logs/${app}.log" 2>&1 &
    echo \$! > "$DIR/data/${app}.pid"
    echo "  [started] pid=\$(cat $DIR/data/${app}.pid) $app"
REMOTE
  echo "<== $remote_host $app ok"; echo
done
echo "[deploy] 全部完成"
