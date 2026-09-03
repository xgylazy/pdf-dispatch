#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"
BUILD_DIR="${PROJ}/build"
DIST_DIR="${PROJ}/dist/standalone"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"

echo "════════════════════════════════════════════════════"
echo "  pdf-dispatch 绿色包 v${VERSION}"
echo "════════════════════════════════════════════════════"

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"/{models,data,logs,scripts}

# ── Step 1: 拉取 python-build-standalone ────────────
PY_DIR="${BUILD_DIR}/python311"
if [[ ! -d "$PY_DIR" ]]; then
  echo "[1/5] 下载 python-build-standalone (Python 3.11) ..."
  PY_FN="cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz"
  curl -fL "https://github.com/indygreg/python-build-standalone/releases/download/20240415/${PY_FN}" \
    -o "${BUILD_DIR}/py.tar.gz"
  mkdir -p "$PY_DIR"
  tar -xzf "${BUILD_DIR}/py.tar.gz" -C "$PY_DIR" --strip-components=1
fi
PY_BIN="${PY_DIR}/bin/python3"
"$PY_BIN" --version

# ── Step 2: 构建 venv + 装依赖 ──
echo "[2/5] 创建 venv 并装依赖 (paddlepaddle / paddleocr / httpx / pymupdf)..."
VENV_DIR="${BUILD_DIR}/venv"
"$PY_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install httpx pymupdf -q
"$VENV_DIR/bin/pip" install paddlepaddle paddleocr -q

# ── Step 3: 下载 OCR 模型（离线包内含）───────────────
echo "[3/5] 下载离线 OCR 模型"
"$VENV_DIR/bin/python" - <<PYEOF
import os
os.environ["PADDLE_PDX_CACHE_HOME"] = "${DIST_DIR}/models"
from paddleocr import PaddleOCR
for m in ["PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"]:
    try:
        PaddleOCR(text_detection_model_name=m, text_recognition_model_name=m,
                  use_doc_orientation_classify=False, use_doc_unwarping=False,
                  use_textline_orientation=False, enable_mkldnn=True)
    except Exception:
        PaddleOCR()
print("models -> ${DIST_DIR}/models")
PYEOF

# ── Step 4: 组装正式分发目录 ─────────────────────────
echo "[4/5] 组装绿色包"
cp -r "$VENV_DIR" "$DIST_DIR/venv"
mkdir -p "$DIST_DIR/pdf_dispatch"
cp -r "$PROJ/scheduler" "$PROJ/worker" "$PROJ/shared" "$DIST_DIR/pdf_dispatch/"
cp "$PROJ/requirements.txt" "$PROJ/VERSION" "$DIST_DIR/" 2>/dev/null || true

cat > "$DIST_DIR/start_scheduler.sh" <<'SCHED'
#!/bin/bash
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"
export DATA_DIR="${DATA_DIR:-$DEPLOY/data}"
mkdir -p "$DATA_DIR"
cd "$DEPLOY/pdf_dispatch"
exec "$DEPLOY/venv/bin/python" -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
SCHED

cat > "$DIST_DIR/start_worker.sh" <<'WORKER'
#!/bin/bash
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
export CAPACITY="${CAPACITY:-0}"
export SCHEDULER_URL="${SCHEDULER_URL:-http://10.0.0.10:8000}"
cd "$DEPLOY/pdf_dispatch"
exec "$DEPLOY/venv/bin/python" -m worker.main "$@"
WORKER

chmod +x "$DIST_DIR/start_scheduler.sh" "$DIST_DIR/start_worker.sh"

# 把构建脚本本身也拷进去（供用户未来重新打包参考）
cp "$PROJ/scripts/"*.sh "$DIST_DIR/scripts/" 2>/dev/null || true
chmod +x "$DIST_DIR/scripts/"*.sh 2>/dev/null || true

echo "[done] 绿色包已生成: $DIST_DIR  ($(du -sh "$DIST_DIR" | cut -f1))"
echo ""
echo "分发命令:"
echo "  tar -czf pdf-distribute-${VERSION}.tar.gz -C $DIST_DIR ."
echo "使用目标机:"
echo "  tar -xzf pdf-distribute-${VERSION}.tar.gz -C /opt/pdf-distribute"
echo "  ./start_worker.sh     # worker（自检 CPU，无端口）"
echo "  ./start_scheduler.sh   # 调度中心（端口 8000）"
