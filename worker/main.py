# -*- coding: utf-8 -*-
"""无状态解析后端（纯 asyncio，无端口、无 FastAPI、无 uvicorn）。

职责：启动后连调度中心 claim 任务 → 拉取预切 PDF 片段 → 解析 → 回调结果。
节点无端口、对外不暴露 HTTP，仅通过 httpx 客户端主动连接调度中心。

容量自动感知：CAPACITY 默认取本机 CPU 核数，可通过环境变量覆盖。
解析引擎注入：ENGINE_MODULE 环境变量指定一个 python 模块路径，
该模块必须暴露 parse_pdf(bytes) -> list[dict]。
默认 "pdf_dispatch.worker.engine"（pymupdf 纯矢量）。
装 pdf2tree 后改为 "pdf2tree.app.core.extract"。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
import uuid

import httpx
from fastapi import FastAPI

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
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://localhost:28765")
BACKEND_ID = os.getenv("BACKEND_ID") or f"worker-{uuid.uuid4().hex[:12]}"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10.0"))
ENGINE_MODULE = os.getenv("ENGINE_MODULE", "pdf_dispatch.worker.engine")
CAPACITY = int(os.getenv("CAPACITY") or (os.cpu_count() or 4))

WORKER_PID = os.getpid()
_WORKER_IP = ""

def _get_worker_ip() -> str:
    global _WORKER_IP
    if _WORKER_IP:
        return _WORKER_IP
    s = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        _WORKER_IP = s.getsockname()[0]
    except OSError:
        _WORKER_IP = "127.0.0.1"
    finally:
        s.close()
    return _WORKER_IP

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


# 解析专用常驻子进程（单进程即可：主循环本身串行领任务）。
# 子进程里 paddle 模型只加载一次并常驻，后续任务直接复用；
# 任务被取消时父进程直接 kill 子进程（独立 GIL，主进程心跳不受任何影响）。
_MP_CTX = mp.get_context("spawn")
_parse_proc: mp.Process | None = None
_parse_cmd_conn = None
_parse_res_conn = None


def _parse_server(cmd_conn, res_conn) -> None:
    """子进程主循环：收 (piece, offset) → 解析 → 回 (status, payload)。"""
    while True:
        try:
            cmd = cmd_conn.recv()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd is None:
            break
        piece, offset = cmd
        try:
            res_conn.send(("ok", _engine_parse_pdf(piece, page_offset=offset)))
        except BaseException as e:      # noqa: BLE001  解析崩溃不能带走子进程循环
            try:
                res_conn.send(("error", str(e)[:500]))
            except Exception:
                pass


def _ensure_parse_proc() -> None:
    global _parse_proc, _parse_cmd_conn, _parse_res_conn
    if _parse_proc is not None and _parse_proc.is_alive():
        return
    cmd_parent, cmd_child = _MP_CTX.Pipe(duplex=True)
    res_parent, res_child = _MP_CTX.Pipe(duplex=False)
    p = _MP_CTX.Process(target=_parse_server, args=(cmd_child, res_child),
                        daemon=True)
    p.start()
    cmd_child.close()
    res_child.close()
    _parse_proc, _parse_cmd_conn, _parse_res_conn = p, cmd_parent, res_parent


def _kill_parse_proc() -> None:
    global _parse_proc, _parse_cmd_conn, _parse_res_conn
    try:
        if _parse_proc is not None:
            _parse_proc.kill()
            _parse_proc.join(timeout=5)
    except Exception:
        pass
    _parse_proc = None
    _parse_cmd_conn = None
    _parse_res_conn = None


# ---------------------------------------------------------------------------
# 取消指令接收端口（scheduler 推送 POST /abort/{task_id}）
# ---------------------------------------------------------------------------
WORKER_PORT = int(os.getenv("WORKER_PORT", "28766"))

app = FastAPI(title="pdf-dispatch worker")
_current: dict = {"task_id": None, "abort": False}


@app.post("/abort/{task_id}")
async def _abort_task(task_id: str):
    """scheduler 取消任务时推送：让正在解析的子进程立刻终止。"""
    if _current.get("task_id") == task_id:
        _current["abort"] = True
    return {"ok": True, "backend_id": BACKEND_ID, "aborted": _current["abort"]}


@app.get("/")
async def _root():
    return {"backend_id": BACKEND_ID, "pid": WORKER_PID,
            "current_task": _current.get("task_id"),
            "abort": _current.get("abort", False)}

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
                               json={"backend_id": BACKEND_ID, "ip": _get_worker_ip(), "pid": WORKER_PID,
                                     "url": f"http://{_get_worker_ip()}:{WORKER_PORT}",
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
    global _active_tasks
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{SCHEDULER_URL}/internal/claim",
                           json={"backend_id": BACKEND_ID, "ip": _get_worker_ip(), "pid": WORKER_PID})
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

        # 两步式：claim 给 pdf_url，原始二进制拉取（已是预切片，无需再裁）
        pr = await cli.get(f"{SCHEDULER_URL}{body['pdf_url']}")
        pr.raise_for_status()
        piece = pr.content
        t0 = time.time()
        # 解析放到【子进程】跑：paddle 推理会占住 GIL，线程/事件循环都会被
        # 冻住（心跳停发 → 调度中心误判不健康）。子进程有独立 GIL，父进程
        # 的心跳/claim 循环全程畅通。
        # 取消：scheduler 推 POST /abort/{task_id} → 置 abort 标志 → kill 子进程。
        _ensure_parse_proc()
        _current["task_id"] = task_id
        _current["abort"] = False
        _parse_cmd_conn.send((piece, page_start - 1))
        loop = asyncio.get_running_loop()
        aborted = False
        result = None
        while True:
            got = await loop.run_in_executor(None, _parse_res_conn.poll, 1.0)
            if got or _current["abort"]:
                break
        if _current["abort"]:
            aborted = True
        else:
            try:
                status, payload = _parse_res_conn.recv()
                result = payload if status == "ok" else None
                err = None if status == "ok" else str(payload)
            except EOFError:
                err = "parse child died"
        parse_ms = int((time.time() - t0) * 1000)
        if aborted:
            _kill_parse_proc()
            records, ok, err = [], False, "cancelled"
            log.info("parse aborted by scheduler: %s", task_id)
        elif result is not None:
            records, ok = result, True
            log.info("parsed %s OK (%d records, %.1fs)",
                     task_id, len(records), time.time() - t0)
        else:
            records, ok = [], False
            log.error("parse failed %s: %s", task_id, err)
        _current["task_id"] = None
        _current["abort"] = False

        cb = {"task_id": task_id, "job_id": body["job_id"],
              "chunk_index": body["chunk_index"], "page_start": page_start,
                    "page_end": page_end, "backend_id": BACKEND_ID,
                    "ip": _get_worker_ip(), "pid": WORKER_PID,
              "ok": ok, "records": records, "error": err, "parse_ms": parse_ms}
        try:
            await cli.post(f"{SCHEDULER_URL}/internal/task_done", json=cb)
        except Exception:
            log.exception("task_done push failed for %s", task_id)

        async with _lock:
            _active_tasks = max(0, _active_tasks - 1)
        return True


async def _main_loop() -> None:
    # 取消指令接收端口（与主循环同一事件循环，/abort 处理器直接改共享状态）
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=WORKER_PORT,
                            log_level="warning")
    uv_server = uvicorn.Server(config)
    uv_server.install_signal_handlers = lambda: None   # 信号交给 worker 自己处理
    asyncio.create_task(uv_server.serve())
    asyncio.create_task(_heartbeat_loop())
    log.info("main loop started (abort port %d)", WORKER_PORT)
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
