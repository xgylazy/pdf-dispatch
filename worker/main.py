# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""无状态解析后端（纯 asyncio，无端口、无 FastAPI、无 uvicorn）。

职责：启动后连调度中心 pull 任务 → 裁页 → 解析 → 回调结果。
节点无端口、对外不暴露 HTTP，仅通过 httpx 客户端主动连接调度中心。

容量自动感知：CAPACITY 默认取本机 CPU 核数，可通过环境变量覆盖。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import signal
import sys
import time

import httpx
import pymupdf

from shared.pdfsplit import extract_page_range

# ---------------------------------------------------------------------------
# 日志（无 uvicorn 接管，自己配置到 stderr / 文件）
# ---------------------------------------------------------------------------
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://localhost:8000")
BACKEND_ID = os.getenv("BACKEND_ID", f"worker-{os.getpid()}")
PDF2TREE_PATH = os.getenv("PDF2TREE_PATH", "")
OCR_MODEL_DIR = os.getenv("OCR_MODEL_DIR", "")   # 离线 OCR 模型；设了就禁下载
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10.0"))

# 并发能力自动感知：默认 CPU 核数，最低 1；CAPACITY 环境变量可覆盖
CAPACITY = int(os.getenv("CAPACITY") or (os.cpu_count() or 4))
_active_tasks: int = 0
_lock = asyncio.Lock()   # 保护 _active_tasks

log.info("worker starting: capacity=%d  scheduler=%s", CAPACITY, SCHEDULER_URL)

# ---------------------------------------------------------------------------
# 解析引擎
# ---------------------------------------------------------------------------

def _parse_with_pdf2tree(pdf_bytes: bytes) -> list[dict]:
    """调 pdf2tree 的双路解析（矢量 + 扫描页 OCR）。"""
    import sys as _sys
    if PDF2TREE_PATH and PDF2TREE_PATH not in _sys.path:
        _sys.path.insert(0, PDF2TREE_PATH)
    from app.core.extract import extract_records
    from app.config import Settings as _Settings

    env_path = os.path.join(PDF2TREE_PATH, ".env")
    settings = _Settings(_env_file=env_path) if os.path.exists(env_path) else _Settings()
    if OCR_MODEL_DIR:
        settings.ocr_model_dir = OCR_MODEL_DIR
    records, _, _ = extract_records(pdf_bytes, settings, ocr_mode="auto")
    return records


def _parse_pymupdf_fallback(pdf_bytes: bytes) -> list[dict]:
    """无 pdf2tree 时的退化：纯 pymupdf 矢量提取（无 OCR）。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out: list[dict] = []
    ph = (doc[0].rect.height if doc.page_count else 1.0) or 1.0
    for i in range(doc.page_count):
        out.append({"t": "page", "page": i + 1})
        d = doc[i].get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type", 0) != 0:
                continue
            for line in b.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if text.strip():
                    bbox = pymupdf.Rect(line["bbox"])
                    out.append({"t": "line", "page": i + 1,
                                "y": bbox.y0, "x": bbox.x0,
                                "yf": bbox.y0 / ph, "text": text})
    doc.close()
    return out


def parse_pdf(pdf_bytes: bytes) -> list[dict]:
    """根据环境选择解析引擎。"""
    if PDF2TREE_PATH and os.path.isdir(PDF2TREE_PATH):
        try:
            return _parse_with_pdf2tree(pdf_bytes)
        except Exception:
            log.exception("pdf2tree engine failed, fallback to pymupdf")
    return _parse_pymupdf_fallback(pdf_bytes)


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

async def _heartbeat_loop() -> None:
    """周期上报本机能力与当前负载给调度中心。"""
    async with httpx.AsyncClient(timeout=10) as cli:
        while True:
            try:
                async with _lock:
                    current_active = _active_tasks
                await cli.post(f"{SCHEDULER_URL}/internal/heartbeat",
                               json={"backend_id": BACKEND_ID,
                                     "url": BACKEND_ID,
                                     "capacity": CAPACITY,
                                     "active_tasks": current_active,
                                     "pdf_capable": bool(PDF2TREE_PATH)})
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# 主循环：pull → 解析 → push
# ---------------------------------------------------------------------------

async def _tick() -> bool:
    """向调度中心拉取一个分片并处理；返回是否拿到任务。"""
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{SCHEDULER_URL}/internal/claim",
                           json={"backend_id": BACKEND_ID})
        if r.status_code in (204, 404):
            return False
        if r.status_code != 200:
            log.warn("claim unexpected status %s", r.status_code)
            return False
        body = r.json()

        # 通知调度中心我开始忙了
        async with _lock:
            _active_tasks += 1

        task_id = body["task_id"]
        page_start = body["page_start"]
        page_end = body["page_end"]
        log.info("claimed %s (pages %d-%d)", task_id, page_start, page_end)

        raw = base64.b64decode(body["pdf_bytes"])
        piece = extract_page_range(raw, page_start, page_end)
        t0 = time.time()
        try:
            records = parse_pdf(piece)
            ok, err = True, None
            log.info("parsed %s OK (%d records, %.1fs)",
                     task_id, len(records), time.time() - t0)
        except Exception as e:
            records, ok, err = [], False, str(e)[:300]
            log.exception("parse failed %s: %s", task_id, err)
        parse_ms = int((time.time() - t0) * 1000)

        cb = {"task_id": task_id,
              "job_id": body["job_id"],
              "chunk_index": body["chunk_index"],
              "page_start": page_start,
              "page_end": page_end,
              "backend_id": BACKEND_ID,
              "ok": ok, "records": records,
              "error": err, "parse_ms": parse_ms}
        try:
            await cli.post(f"{SCHEDULER_URL}/internal/task_done", json=cb)
        except Exception:
            log.exception("task_done push failed for %s", task_id)

        # 干完了，松口气
        async with _lock:
            _active_tasks = max(0, _active_tasks - 1)
        return True


async def _main_loop() -> None:
    """Worker 生命周期：周期性 pull 任务直到被信号终止。"""
    asyncio.create_task(_heartbeat_loop())
    log.info("main loop started (poll_interval=%.1fs)", POLL_INTERVAL)
    while True:
        try:
            got = await _tick()
        except Exception:
            log.exception("tick failed")
            got = True
        if not got:
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _shutdown(signum, frame):
    log.info("received signal %s, shutting down", signum)
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    asyncio.run(_main_loop())


if __name__ == "__main__":
    main()
