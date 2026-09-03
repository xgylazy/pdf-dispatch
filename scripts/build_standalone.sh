#!/bin/bash
# -*- coding: utf-8 -*-
# 在【能联网的 Linux 构建机】一次性产出"绿色包"：Python 解释器 + venv 依赖 + 模型 + 应用代码，
# 打包进同一个目录。之后 scp 到任何 Linux 机器、解压即可运行——无需目标机有任何 Python / 网络。
#
# 依赖：curl、tar（构建机自带）
# 运行： ./scripts/build_standalone.sh
# 产出： dist/standalone/ 目录 → 交给 ./scripts/package.sh 打 tarball
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"       # pdf_dispatch 工程根
BUILD_DIR="${PROJ}/build"
DIST_DIR="${PROJ}/dist/standalone"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"

# ── 参数 ────────────────────────────────────────────
PY_VER="3.11"
PY_RELEASE="20240415"
PLATFORM="x86_64-unknown-linux-gnu"
BUCKET="https://github.com/indygreg/python-build-standalone/releases/download"

echo "═══════════════════════════════════════════════════════"
echo "  构建绿色包 v${VERSION}"
echo "  目标平台: ${PLATFORM}  Python ${PY_VER}"
echo "═══════════════════════════════════════════════════════"

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"/{models,data,logs}

# ── Step 1: 拉取 python-build-standalone ────────────
PY_DIR="${BUILD_DIR}/python311"
if [[ ! -d "$PY_DIR" ]]; then
  echo "[1/5] 下载 python-build-standalone (Python ${PY_VER}) ..."
  FN="cpython-${PY_VER}+${PY_RELEASE}-${PLATFORM}-install_only.tar.gz"
  curl -fL "${BUCKET}/${PY_RELEASE}/${FN}" -o "${BUILD_DIR}/py.tar.gz"
  mkdir -p "$PY_DIR"
  tar -xzf "${BUILD_DIR}/py.tar.gz" -C "$PY_DIR" --strip-components=1
fi
PY_BIN="${PY_DIR}/bin/python3"
"$PY_BIN" --version

# ── Step 2: 用 standalone python 建 venv + 装依赖 ──
echo "[2/5] 创建 venv 并安装依赖 (paddlepaddle / paddleocr / fastapi / pymupdf) ..."
VENV_DIR="${BUILD_DIR}/venv"
"$PY_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install \
    fastapi "uvicorn[standard]" httpx pydantic pymupdf -q
# paddlepaddle + paddleocr（CPU 版；GPU 版自行换成 paddlepaddle-gpu）
"$VENV_DIR/bin/pip" install paddlepaddle paddleocr -q

# ── Step 3: 下载 OCR 模型（离线包内含）───────────────
echo "[3/5] 下载离线 OCR 模型"
"$VENV_DIR/bin/python" "$PROJ/../pdf2tree/scripts/download_models.py" \
    --dest "${DIST_DIR}/models" --resume

# ── Step 4: 组装正式分发目录 ────────────────────────
echo "[4/5] 组装绿色包目录"
# 拷 venv（含 bin/python + lib/python3.11/site-packages）
cp -r "$VENV_DIR" "$DIST_DIR/venv"
# pdf2tree 引擎代码（worker 运行时 sys.path.insert 加载）
[[ -d "$PROJ/../pdf2tree/app" ]] && cp -r "$PROJ/../pdf2tree" "$DIST_DIR/pdf2tree"
# pdf_dis{͏patch} 应用代码
cp -r "$PROJ/scheduler" "$PROJ/worker" "$PROJ/shared" "$DIST_DIR/"
cp "$PROJ/requirements.txt" "$PROJ/VERSION" "$DIST_DIR/" 2>/dev/null || true

# ── Step 5: 写启动脚本（容器/物理机通用）─────────────
cat > "$DIST_DIR/start_scheduler.sh" <<'START_EOF'
#!/bin/bash
# 调度中心启动脚本——无需外部 Python / 网络
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PYTHONPATH="$DEPLOY:$DEPLOY/pdf2tree:$PYTHONPATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"     # 离线 OCR 模型目录
export DATA_DIR="${DATA_DIR:-$DEPLOY/data}"
mkdir -p "$DATA_DIR"
cd "$DEPLOY"
exec "$DEPLOY/venv/bin/uvicorn" scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
START_EOF

cat > "$DIST_DIR/start_worker.sh" <<'WORKER_EOF'
#!/bin/bash
# worker 启动脚本——无需外部 Python / 网络 / 端口
# 纯 asyncio 事件循环，仅通过 httpx 主动连接调度中心
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PYTHONPATH="$DEPLOY:$DEPLOY/pdf2tree:$PYTHONPATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"     # 离线 OCR 模型
export PDF2TREE_PATH="$DEPLOY/pdf2tree"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
export CAPACITY="${CAPACITY:-0}"                   # 0 表示自动取 CPU 核数
export SCHEDULER_URL="${SCHEDULER_URL:-http://10.0.0.10:8000}"
cd "$DEPLOY"
exec "$DEPLOY/venv/bin/python" -m worker.main "$@"
WORKER_EOF

chmod +x "$DIST_DIR/start_scheduler.sh" "$DIST_DIR/start_worker.sh"
echo "[5/5] 完成。产出: $DIST_DIR  ($(du -sh "$DIST_DIR" | cut -f1))"
echo ""
echo "下一步:"
echo "  1) cd $PROJ && ./scripts/package.sh         # -> dist/pdf-distribute-${VERSION}.tar.gz"
echo "  2) ./scripts/deploy.sh servers.txt          # scp 到目标机 + 自动解压 + 启动"
