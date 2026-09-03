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

echo "[deploy] worker=$PKG_WK  scheduler=$PKG_SC  targets=${#TARGETS[@]}"
for t in "${TARGETS[@]}"; do echo "  $t"; done; echo

for target in "${TARGETS[@]}"; do
  host="${target%% *}"; kv="${target#* }"
  app="worker"
  for kvpair in $kv; do
    case "$kvpair" in app=*) app="${kvpair#app=}" ;; esac
  done

  if [[ "$app" == "scheduler" ]]; then
    PKG="$PKG_SC"; DIR="$DIR_SC"
  else
    PKG="$PKG_WK"; DIR="$DIR_WK"
  fi
  echo "==> $host ($app -> $DIR)"

  scp $SSH_OPTS "$PKG" "$host:/tmp/$(basename "$PKG")"
  ssh $SSH_OPTS "$host" bash <<REMOTE
    set -uo pipefail
    mkdir -p "$DIR"
    tar -xzf "/tmp/$(basename "$PKG")" -C "$DIR"
    cd "$DIR"
    nohup ./start.sh > "$DIR/data/logs/${app}.log" 2>&1 &
    echo \$! > "$DIR/data/${app}.pid"
    echo "  [started] pid=\$(cat $DIR/data/${app}.pid) $app"
REMOTE
  echo "<== $host $app ok"; echo
done
echo "[deploy] 全部完成"
