# -*- coding: utf-8 -*-
"""无状态解析后端（纯 asyncio，无端口、无 FastAPI、无 uvicorn）。

职责：启动后连调度中心 pull 任务 → 裁页 → 解析 → 回调结果。
节点无端口、对外不暴露 HTTP，仅通过 httpx 客户端主动连接调度中心。

容量自动感知：CAPACITY 默认取本机 CPU 核数，可通过环境变量覆盖。
解析引擎注入：ENGINE_MODULE 环境变量指定一个 python 模块路径，
该模块必须暴露 parse_pdf(bytes) -> list[dict]。
默认 "pdf_dispatch.worker.engine"（pymupdf 纯矢量）。
装 pdf2tree 后改为 "pdf2tree.app.core.extract"。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import os
import signal
import sys
import time

import httpx

from shared.pdfsplit import extract_page_range

# ---------------------------------------------------------------------------
# 日志
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
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10.0"))
ENGINE_MODULE = os.getenv("ENGINE_MODULE", "pdf_dispatch.worker.engine")
CAPACITY = int(os.getenv("CAPACITY") or (os.cpu_count() or 4))

_active_tasks: int = 0
_lock = asyncio.Lock()

# 加载解析引擎（启动时一次性，失败则退出）
try:
    _mod = importlib.import_module(ENGINE_MODULE)
    _engine_parse_pdf = _mod.parse_pdf
    log.info("engine loaded: %s", ENGINE_MODULE)
except Exception as e:
    log.error("failed to parse ENGINE_MODULE=%s: %s", ENGINE_MODULE, e)
    sys.exit(1)

def _engine_has_ocr() -> bool:
    """启发式判断引擎是否支持 OCR：模块路径含 paddle 或 pdf2tree 视为 True。"""
    return "paddle" in ENGINE_MODULE or "pdf2tree" in ENGINE_MODULE

# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

async def _heartbeat_loop() -> None:
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
                                     "pdf_capable": _engine_has_ocr()})
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def _tick() -> bool:
    nonlocal _active_tasks
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{SCHEDULER_URL}/internal/claim",
                           json={"backend_id": BACKEND_ID})
        if r.status_code in (204, 404):
            return False
        if r.status_code != 200:
            log.warning("claim unexpected status %s", r.status_code)
            return False
        body = r.json()

        async with _lock:
            _active_tasks += 1
        task_id = body["task_id"]
        page_start = body["page_start"]
        page_end = body["page_end"]
        log.info("claimed %s (pages %d-%d)", task_id, page_start, page_end)

        pdf_bytes = base64.b64decode(body["pdf_bytes"])
        piece = extract_page_range(pdf_bytes, page_start, page_end)
        t0 = time.time()
        try:
            records = _engine_parse_pdf(piece)
            ok, err = True, None
            log.info("parsed %s OK (%d records, %.1fs)",
                     task_id, len(records), time.time() - t0)
        except Exception as e:
            records, ok, err = [], False, str(e)[:300]
            log.exception("parse failed %s: %s", task_id, err)
        parse_ms = int((time.time() - t0) * 1000)

        cb = {"task_id": task_id, "job_id": body["job_id"],
              "chunk_index": body["chunk_index"], "page_start": page_start,
              "page_end": page_end, "backend_id": BACKEND_ID,
              "ok": ok, "records": records, "error": err, "parse_ms": parse_ms}
        try:
            await cli.post(f"{SCHEDULER_URL}/internal/task_done", json=cb)
        except Exception:
            log.exception("task_done push failed for %s", task_id)

        async with _lock:
            _active_tasks = max(0, _active_tasks - 1)
        return True


async def _main_loop() -> None:
    asyncio.create_task(_heartbeat_loop())
    log.info("main loop started")
    while True:
        try:
            got = await _tick()
        except Exception:
            log.exception("tick failed")
            got = True
        if not got:
            await asyncio.sleep(POLL_INTERVAL)


def _shutdown(signum, frame):
    log.info("received signal %s", signum)
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    asyncio.run(_main_loop())


if __name__ == "__main__":
    main()
