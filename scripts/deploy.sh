#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 0.1.6)}"
PKG_WK="dist/pdf-distribute-worker-${VERSION}.tar.gz"
PKG_SC="dist/pdf-distribute-scheduler-${VERSION}.tar.gz"
DIR_WK="/opt/pdf-worker"
DIR_SC="/opt/pdf-scheduler"

# ---------- 自动准备产物 ----------
# 如果 dist/ 下还没有当前 VERSION 的 tarball，就自动找 zip 解压
# 搜索顺序：先 dist/ 后脚本所在目录（项目根目录）；zip 在哪就解压到哪
if [[ ! -f "$PKG_WK" || ! -f "$PKG_SC" ]]; then
  echo "==> [deploy] v${VERSION} 产物不在 dist/ 下，搜索 zip..."
  ZIP=""
  for candidate in "dist/pdf-distribute-v${VERSION}.zip" "pdf-distribute-v${VERSION}.zip"; do
    if [[ -f "$candidate" ]]; then ZIP="$candidate"; break; fi
  done
  if [[ -z "$ZIP" ]]; then
    echo
    echo "==> [ERROR] 找不到 pdf-distribute-v${VERSION}.zip"
    echo "    已搜索路径："
    echo "      - $(pwd)/dist/pdf-distribute-v${VERSION}.zip"
    echo "      - $(pwd)/pdf-distribute-v${VERSION}.zip"
    echo "    解决方式：把 zip 放到上面任一位置，然后重新执行："
    echo "      bash scripts/deploy.sh servers.txt"
    exit 2
  fi
  echo "==> [deploy] 找到 $ZIP，解压到 dist/..."
  mkdir -p dist
  unzip -o "$ZIP" -d dist/
  [[ -f "$PKG_WK" && -f "$PKG_SC" ]] || { echo "[ERROR] 解压后仍缺少 tarball"; exit 2; }
fi
echo "==> [deploy] 产物就绪：$PKG_WK + $PKG_SC"

[[ -f "${1:-}" ]] || { echo "[ERROR] 需要 servers.txt 路径（用法: $0 servers.txt）"; exit 2; }

SSH_OPTS="${SSH_OPTS:--i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=10}"
mapfile -t TARGETS < <(grep -vE '^\s*#|^\s*$' "$1")

# 从 servers.txt 里找 scheduler IP，worker 自动连这个 IP
SCHEDULER_IP=""
for t in "${TARGETS[@]}"; do
  host="${t%% *}"; rest="${t#* }"
  for kv in $rest; do
    [[ "$kv" == "app=scheduler" ]] && SCHEDULER_IP="${host#*@}" && break 2
  done
done
[[ -n "$SCHEDULER_IP" ]] || { echo "[ERROR] servers.txt 里没找到 app=scheduler 的行"; exit 2; }
export SCHEDULER_URL="http://${SCHEDULER_IP}:28765"
echo "[deploy] scheduler=$SCHEDULER_URL  targets=${#TARGETS[@]}"
for t in "${TARGETS[@]}"; do echo "  $t"; done; echo

for target in "${TARGETS[@]}"; do
  host="${target%% *}"; rest="${target#* }"
  app="worker"
  for kv in $rest; do
    case "$kv" in app=*) app="${kv#app=}" ;; esac
  done
  if [[ "$app" == "scheduler" ]]; then
    PKG="$PKG_SC"; DIR="$DIR_SC"
  else
    PKG="$PKG_WK"; DIR="$DIR_WK"
  fi
  echo "==> $host ($app -> $DIR, scheduler=$SCHEDULER_URL)"

  scp $SSH_OPTS "$PKG" "$host:/tmp/$(basename "$PKG")"
  REMOTE_ENV=""
  [[ "$app" == "worker" ]] && REMOTE_ENV="SCHEDULER_URL=$SCHEDULER_URL"

  ssh $SSH_OPTS "$host" bash <<REMOTE
    set -uo pipefail
    mkdir -p "$DIR/data/logs"
    # 停掉旧进程（如果在跑）
    if [[ -f "$DIR/data/${app}.pid" ]] && kill -0 "\$(cat "$DIR/data/${app}.pid")" 2>/dev/null; then
      kill "\$(cat "$DIR/data/${app}.pid")" 2>/dev/null || true
      sleep 1
    fi
    tar -xzf "/tmp/$(basename "$PKG")" -C "$DIR" || exit 1
    cd "$DIR" || exit 1
    # 清理历史版本/手工部署的残留目录（当前包结构已不含这些）
    [[ "\$PWD" == "$DIR" ]] && rm -rf venv bin lib site-packages
    # 新部署清理旧的 jobs/tasks，避免 scheduler recover() 重新载入历史状态
    rm -f "$DIR"/data/jobs/*.json "$DIR"/data/tasks/*.json
    $REMOTE_ENV nohup ./start.sh > "$DIR/data/logs/${app}.log" 2>&1 &
    echo \$! > "$DIR/data/${app}.pid"
    sleep 3
    # 存活检查：启动后 3 秒进程还在才算成功
    if kill -0 "\$(cat "$DIR/data/${app}.pid")" 2>/dev/null; then
      echo "  [started] pid=\$(cat "$DIR/data/${app}.pid") $app"
    else
      echo "  [FAILED] $app 启动后 3 秒内退出，最近日志："
      tail -n 20 "$DIR/data/logs/${app}.log" || true
      exit 1
    fi
REMOTE
  echo "<== $host $app ok"; echo
done
echo "[deploy] 全部完成"
