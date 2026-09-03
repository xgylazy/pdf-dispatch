#!/bin/bash
# -*- coding: utf-8 -*-
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$HERE")"
BUILD_DIR="${PROJ}/build"
DIST_DIR="${PROJ}/dist/standalone"
VERSION="${VERSION:-$(cat "$PROJ/VERSION" 2>/dev/null || echo 0.1.0)}"

echo "══════════════════════════════════════════════════════════════════"
echo "  pdf-dispatch 绿色包 v${VERSION}"
echo "  独立工程（无 pdf2tree 依赖）；解析引擎由目标环境 ENGINE_MODULE 注入"
echo "══════════════════════════════════════════════════════════════════"

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"/{models,data,logs}

# ── Step 1: 拉取 python-build-standalone ────────────
PY_DIR="${BUILD_DIR}/python311"
if [[ ! -d "$PY_DIR" ]]; then
  echo "[1/4] 下载 python-build-standalone (Python 3.11) ..."
  PY_FN="cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz"
  curl -fL "https://github.com/indygreg/python-build-standalone/releases/download/20240415/${PY_FN}" \
    -o "${BUILD_DIR}/py.tar.gz"
  mkdir -p "$PY_DIR"
  tar -xzf "${BUILD_DIR}/py.tar.gz" -C "$PY_DIR" --strip-components=1
fi
PY_BIN="${PY_DIR}/bin/python3"
"$PY_BIN" --version

# ── Step 2: 构建 venv + 装依赖 ──
echo "[2/4] 构建 venv + 装依赖 (paddlepaddle / paddleocr / httpx / pymupdf) ..."
VENV_DIR="${BUILD_DIR}/venv"
"$PY_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"# pdf-dispatch 绿色包：调度框架（无 pdf2tree 依赖）"
"$VENV_DIR/bin/pip" install httpx pymupdf -q
"$VENV_DIR/bin/pip" install paddlepaddle paddleocr -q

# ── Step 3: 下载 OCR 模型（离线包内含）───────────────
echo "[3/4] 下载离线 OCR 模型"
"$VENV_DIR/bin/python" - <<PYEOF
import os
os.environ["PADDLE_PDX_CACHE_HOME"] = "${DIST_DIR}/models"
from paddleocr import PaddleOCR
# 触发下载 4 套常用模型
for m in ["PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"]:
    try:
        PaddleOCR(text_detection_model_name=m, text_recognition_model_name=m,
                  use_doc_orientation_classify=False, use_doc_unwarping=False,
                  use_textline_orientation=False, enable_mkldnn=True)
    except Exception:
        PaddleOCR()
print("models downloaded -> ${DIST_DIR}/models")
PYEOF

# ── Step 4: 组装分发目录 ───────────────────────────
echo "[4/4] 组装绿色包目录"
cp -r "$VENV_DIR" "$DIST_DIR/venv"
mkdir -p "$DIST_DIR/pdf_dispatch"
cp -r "$PROJ/scheduler" "$PROJ/worker" "$PROJ/shared" "$DIST_DIR/pdf_dispatch/"
cp "$PROJ/requirements.txt" "$PROJ/VERSION" "$PROJ/servers.txt.example" "$DIST_DIR/" 2>/dev/null || true

cat > "$DIST_DIR/start_scheduler.sh" <<'START_EOF'
#!/bin/bash
# 调度中心启动脚本——无需外部 Python / 网络
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"
export DATA_DIR="${DATA_DIR:-$DEPLOY/data}"
mkdir -p "$DATA_DIR"
cd "$DEPLOY/pdf_dispatch"
exec "$DEPLOY/venv/bin/python" -m uvicorn scheduler.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
START_EOF

cat > "$DIST_DIR/start_worker.sh" <<'WORKER_EOF'
#!/bin/bash
# worker 启动脚本——纯 asyncio 事件循环，无端口
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
export PATH="$DEPLOY/venv/bin:$PATH"
export PADDLE_PDX_CACHE_HOME="$DEPLOY/models"
# 解析引擎注入：指定一个返回 parse_pdf(bytes)->list[dict] 的 python 模块
# 默认 "pdf_dispatch.worker.engine"（pymupdf 纯矢量兜底，无 OCR）
# 部署 pdf2tree 后改为 "pdf2tree.app.core.extract" 即启用完整 OCR 双路
export ENGINE_MODULE="${ENGINE_MODULE:-pdf_dispatch.worker.engine}"
export BACKEND_ID="${BACKEND_ID:-$(hostname)-worker}"
export CAPACITY="${CAPACITY:-0}"
export SCHEDULER_URL="${SCHEDULER_URL:-http://10.0.0.10:8000}"
cd "$DEPLOY/pdf_dispatch"
exec "$DEPLOY/venv/bin/python" -m worker.main
WORKER_EOF

chmod +x "$DIST_DIR/start_scheduler.sh" "$DIST_DIR/start_worker.sh"
echo "[done] 完成 -> $DIST_DIR  ($(du -sh "$DIST_DIR" | cut -f1))"
echo "提示：部署到已装 pdf2tree 的目标机后，修改 start_worker.sh："
echo "      export ENGINE_MODULE=pdf2tree.app.core.extract"
