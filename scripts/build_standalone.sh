#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"
BUILD_DIR="${PROJ}/build"
DIST_WK="${PROJ}/dist/standalone-worker"
DIST_SC="${PROJ}/dist/standalone-scheduler"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"
echo "[build] VERSION=$VERSION"

echo "════════════════════════════════════════════════════"
echo "  pdf-dispatch 绿色包 v${VERSION}"
echo "════════════════════════════════════════════════════"

rm -rf "$BUILD_DIR" "$DIST_WK" "$DIST_SC"
mkdir -p "$BUILD_DIR"

# ── Step 1: 拉取 python-build-standalone ────────────
PY_DIR="${BUILD_DIR}/python311"
if [[ ! -d "$PY_DIR" ]]; then
  echo "[1/3] 下载 python-build-standalone ..."
  curl -fL "https://github.com/indygreg/python-build-standalone/releases/download/20240415/cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz" \
    -o "${BUILD_DIR}/py.tar.gz"
  mkdir -p "$PY_DIR"
  tar -xzf "${BUILD_DIR}/py.tar.gz" -C "$PY_DIR" --strip-components=1
fi
"${PY_DIR}/bin/python3" --version

# ── Step 2: 复制自带 Python + 装依赖 ────────────────
# 服务器可能没有 python3.11（甚至没有 python），且无法联网。
# 因此把 python-build-standalone 整棵复制进包，依赖直接装进它自己的
# site-packages —— 包在哪都能跑，不依赖系统 Python。
echo "[2/3] 复制自带 Python 并安装依赖"
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
"${PY_DIR}/bin/python3" /tmp/get-pip.py -q

for D in "$DIST_WK" "$DIST_SC"; do
  cp -r "$PY_DIR" "$D/python"
done

# worker 全依赖 → worker/python 自己的 site-packages
"${DIST_WK}/python/bin/python3" -m pip install --no-cache-dir \
  httpx pymupdf paddlepaddle paddleocr -q

# scheduler 基础依赖 → scheduler/python 自己的 site-packages
"${DIST_SC}/python/bin/python3" -m pip install --no-cache-dir \
  httpx pymupdf "uvicorn[standard]" -q

# 验证自带解释器随包可用（防止只拷了依赖没拷解释器的回归）
"${DIST_SC}/python/bin/python3" -c 'import sys, uvicorn, fastapi; print("bundle python OK:", sys.version)' \
  || { echo "[error] 自带 Python 验证失败"; exit 1; }

# OCR 模型
mkdir -p "${DIST_WK}/models"
"${DIST_WK}/python/bin/python3" -c "
import os
os.environ['PADDLE_PDX_CACHE_HOME']='${DIST_WK}/models'
from paddleocr import PaddleOCR
for m in ['PP-OCRv6_medium_det','PP-OCRv6_medium_rec']:
    try:
        PaddleOCR(text_detection_model_name=m,text_recognition_model_name=m,
                  use_doc_orientation_classify=False,use_doc_unwarping=False,
                  use_textline_orientation=False,enable_mkldnn=True)
    except Exception:
        PaddleOCR()
print('models OK')
"

# ── Step 3: 组装代码 + start.sh ──────────────────────
echo "[3/3] 组装"

# worker
cp -r "$PROJ/worker" "$PROJ/shared" "$DIST_WK/"
cat > "$DIST_WK/start.sh" <<'WKSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# 使用包内自带的 Python，不依赖系统解释器（依赖已装进其 site-packages）
export PADDLE_PDX_CACHE_HOME="$HERE/models"
export SCHEDULER_URL="${SCHEDULER_URL:-http://127.0.0.1:8000}"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
cd "$HERE"
exec "$HERE/python/bin/python3.11" -m worker.main
WKSTART

# scheduler
cp -r "$PROJ/scheduler" "$PROJ/shared" "$DIST_SC/"
cat > "$DIST_SC/start.sh" <<'SCSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# 使用包内自带的 Python，不依赖系统解释器
export DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"
cd "$HERE"
exec "$HERE/python/bin/python3.11" -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
SCSTART

chmod +x "$DIST_WK/start.sh" "$DIST_SC/start.sh"
echo "[done]"
echo "  worker:    $DIST_WK ($(du -sh "$DIST_WK" | cut -f1))"
echo "  scheduler: $DIST_SC ($(du -sh "$DIST_SC" | cut -f1))"
