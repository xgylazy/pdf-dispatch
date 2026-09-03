#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"
BUILD_DIR="${PROJ}/build"
DIST_WK="${PROJ}/dist/standalone-worker"
DIST_SC="${PROJ}/dist/standalone-scheduler"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"

echo "════════════════════════════════════════════════════"
echo "  pdf-dispatch 绿色包 v${VERSION}"
echo "════════════════════════════════════════════════════"

rm -rf "$BUILD_DIR" "$DIST_WK" "$DIST_SC"
mkdir -p "$BUILD_DIR"

# ── Step 1: 拉取 python-build-standalone ────────────
PY_DIR="${BUILD_DIR}/python311"
if [[ ! -d "$PY_DIR" ]]; then
  echo "[1/3] 下载 python-build-standalone (Python 3.11.9) ..."
  FN="cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz"
  curl -fL "https://github.com/indygreg/python-build-standalone/releases/download/20240415/$FN" \
    -o "${BUILD_DIR}/py.tar.gz"
  mkdir -p "$PY_DIR"
  tar -xzf "${BUILD_DIR}/py.tar.gz" -C "$PY_DIR" --strip-components=1
fi
"$PY_DIR/bin/python3" --version

# ── Step 2: 构建 venv ───────────────────────────────
echo "[2/3] 构建 venv"

# worker：全依赖
WK_VENV="${BUILD_DIR}/venv-worker"
"${PY_DIR}/bin/python3" -m venv "$WK_VENV"
"$WK_VENV/bin/pip" install --upgrade pip -q
"$WK_VENV/bin/pip" install httpx pymupdf -q
"$WK_VENV/bin/pip" install paddlepaddle paddleocr -q
#        worker 启动时找 OCR 模型
MODELS="$DIST_WK/models"
mkdir -p "$MODELS"
"$WK_VENV/bin/python" -c "
import os
os.environ['PADDLE_PDX_CACHE_HOME'] = '$MODELS'
from paddleocr import PaddleOCR
for m in ['PP-OCRv6_medium_det','PP-OCRv6_medium_rec']:
    try:
        PaddleOCR(text_detection_model_name=m, text_recognition_model_name=m,
                  use_doc_orientation_classify=False, use_doc_unwarping=False,
                  use_textline_orientation=False, enable_mkldnn=True)
    except Exception:
        PaddleOCR()
"

# scheduler：仅基础依赖
SC_VENV="${BUILD_DIR}/venv-scheduler"
"${PY_DIR}/bin/python3" -m venv "$SC_VENV"
"$SC_VENV/bin/pip" install --upgrade pip -q
"$SC_VENV/bin/pip" install httpx pymupdf uvicorn -q

# ── Step 3: 组装目录 ─────────────────────────────────
echo "[3/3] 组装 green 包"

# worker 包
mkdir -p "$DIST_WK/venv" "$DIST_WK/data/logs"
cp -r "$WK_VENV/"* "$DIST_WK/venv/"
cp -r "$PROJ/worker" "$PROJ/shared" "$DIST_WK/"
cat > "$DIST_WK/start.sh" <<'WKSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HERE/venv/bin:$PATH"
export PADDLE_PDX_CACHE_HOME="$HERE/models"
export SCHEDULER_URL="${SCHEDULER_URL:-http://10.0.0.10:8000}"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
cd "$HERE"
exec "$HERE/venv/bin/python" -m worker.main
WKSTART

# scheduler 包
mkdir -p "$DIST_SC/venv" "$DIST_SC/data/logs"
cp -r "$SC_VENV/"* "$DIST_SC/venv/"
cp -r "$PROJ/scheduler" "$PROJ/shared" "$DIST_SC/"
cat > "$DIST_SC/start.sh" <<'SCSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HERE/venv/bin:$PATH"
export DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"
cd "$HERE"
exec "$HERE/venv/bin/python" -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
SCSTART

chmod +x "$DIST_WK/start.sh" "$DIST_SC/start.sh"
echo "[done] worker=$DIST_WK  scheduler=$DIST_SC"
