#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"
BUILD_DIR="${PROJ}/build"
DIST_WK="${PROJ}/dist/standalone-worker"
DIST_SC="${PROJ}/dist/standalone-scheduler"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"
echo "[build] VERSION=$VERSION"

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
echo "  python: $("$PY_DIR/bin/python3" --version)"

# ── Step 2: 直接用 get-pip.py + pip install ──────────
echo "[2/3] 装依赖"
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
"${PY_DIR}/bin/python3" /tmp/get-pip.py -q
"${PY_DIR}/bin/python3" -m pip install --no-cache-dir --upgrade pip -q

# worker：装到 py-build-standalone 自带的 site-packages
WK_SP="${PY_DIR}/lib/python3.11/site-packages"
"${PY_DIR}/bin/python3" -m pip install --no-cache-dir httpx pymupdf paddlepaddle paddleocr -q

# 模型
mkdir -p "${DIST_WK}/models"
PYTHONPATH="$WK_SP" "${PY_DIR}/bin/python3" -c "
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

# scheduler: 独立的精简依赖
SC_SP="${BUILD_DIR}/scheduler-site"
mkdir -p "$SC_SP"
"${PY_DIR}/bin/python3" -m pip install --no-cache-dir --prefix="$SC_SP" httpx pymupdf "uvicorn[standard]" -q

# ── Step 3: 组装 green 包 ────────────────────────────
echo "[3/3] 组装"

# worker
mkdir -p "$DIST_WK/data/logs"
cp -r "$PROJ/worker" "$PROJ/shared" "$DIST_WK/"
cp -r "$WK_SP/." "$DIST_WK/site-packages/"
cat > "$DIST_WK/start.sh" <<'WKSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$HERE/site-packages:${PYTHONPATH:-}"
export PADDLE_PDX_CACHE_HOME="$HERE/models"
export SCHEDULER_URL="${SCHEDULER_URL:-http://127.0.0.1:8000}"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
cd "$HERE"
exec "$HERE/../python-build-standalone/bin/python3" -m worker.main 2>/dev/null || exec python3 -m worker.main
WKSTART

# scheduler
mkdir -p "$DIST_SC/data/logs"
cp -r "$PROJ/scheduler" "$PROJ/shared" "$DIST_SC/"
cp -r "$SC_SP/." "$DIST_SC/"
cat > "$DIST_SC/start.sh" <<'SCSTART'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$HERE/lib/python3.11/site-packages:$HERE/lib64/python3.11/site-packages:${PYTHONPATH:-}"
export DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"
cd "$HERE"
exec python3 -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
SCSTART

chmod +x "$DIST_WK/start.sh" "$DIST_SC/start.sh"
echo "[done] worker=$DIST_WK scheduler=$DIST_SC"
