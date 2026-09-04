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

# ── Step 2: pip install 到各自的 site-packages ────────
echo "[2/3] 装依赖"
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
"${PY_DIR}/bin/python3" /tmp/get-pip.py -q

# worker 全依赖 → dist/standalone-worker/site-packages/
"${PY_DIR}/bin/python3" -m pip install --no-cache-dir \
  --prefix="$DIST_WK" \
  httpx pymupdf paddlepaddle paddleocr -q

# scheduler 基础依赖 → dist/standalone-scheduler/site-packages/
"${PY_DIR}/bin/python3" -m pip install --no-cache-dir \
  --prefix="$DIST_SC" \
  httpx pymupdf "uvicorn[standard]" -q

# OCR 模型
mkdir -p "${DIST_WK}/models"
PYTHONPATH="$DIST_WK/lib/python3.11/site-packages" "${PY_DIR}/bin/python3" -c "
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
rm -rf "$DIST_WK/bin" 2>/dev/null || true
cat > "$DIST_WK/start.sh" <<'WKSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# 自动收集所有 site-packages 目录（lib/ 和 lib64/ 都找）
SP=$(for d in "$HERE"/lib/python*/site-packages "$HERE"/lib64/python*/site-packages; do
       [[ -d "$d" ]] && echo -n "$d:"; done)
export PYTHONPATH="${SP%:}${PYTHONPATH:+:$PYTHONPATH}"
export PADDLE_PDX_CACHE_HOME="$HERE/models"
export SCHEDULER_URL="${SCHEDULER_URL:-http://127.0.0.1:8000}"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
cd "$HERE"
PY=$(command -v python3.11 || command -v python3)
exec "$PY" -m worker.main
WKSTART

# scheduler
cp -r "$PROJ/scheduler" "$PROJ/shared" "$DIST_SC/"
rm -rf "$DIST_SC/bin" 2>/dev/null || true
cat > "$DIST_SC/start.sh" <<'SCSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SP=$(for d in "$HERE"/lib/python*/site-packages "$HERE"/lib64/python*/site-packages; do
       [[ -d "$d" ]] && echo -n "$d:"; done)
export PYTHONPATH="${SP%:}${PYTHONPATH:+:$PYTHONPATH}"
export DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"
cd "$HERE"
PY=$(command -v python3.11 || command -v python3)
exec "$PY" -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
SCSTART

chmod +x "$DIST_WK/start.sh" "$DIST_SC/start.sh"
echo "[done]"
echo "  worker:    $DIST_WK ($(du -sh "$DIST_WK" | cut -f1))"
echo "  scheduler: $DIST_SC ($(du -sh "$DIST_SC" | cut -f1))"
