# -*- coding: utf-8 -*-
"""调度中心 HTTP 入口（FastAPI）。

POST   /jobs               提交 PDF（multipart），202 返回 job_id
GET    /jobs/{job_id}       查询作业状态
GET    /jobs/{job_id}/result 下载合并后的行记录 JSONL
POST   /internal/heartbeat  后端心跳注册
POST   /internal/claim      worker 拉取一个分片
POST   /internal/task_done  worker 完成回调（worker 主动推结果）
"""
from __future__ import annotations

import math
import os
import uuid

import pymupdf
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from scheduler.dispatcher import Dispatcher
from scheduler.persistence import FileStore
from shared.protocol import Heartbeat, TaskCallback

store = FileStore()
dispatcher = Dispatcher(store)
app = FastAPI(title="pdf-dispatch scheduler")

# 分片规则默认值（构建时由 build.conf 烘焙进 start.sh，运行时可被环境变量覆盖）
CHUNK_PAGES_TEXT = int(os.getenv("CHUNK_PAGES_TEXT", "10"))
CHUNK_PAGES_SCANNED = int(os.getenv("CHUNK_PAGES_SCANNED", "1"))
CHUNK_SCANNED_RATIO = float(os.getenv("CHUNK_SCANNED_RATIO", "0.5"))


def _auto_split_size(doc: pymupdf.Document, page_count: int) -> int:
    """按内容自动决定每片页数。

    抽样（最多 30 页）统计"无可提取文字"的页面占比：
      >= CHUNK_SCANNED_RATIO → 视为扫描件，用小片（OCR 慢，并行优先）
      否则                   → 文字页，用大片（矢量解析快）
    判定标准与 worker.engine._is_text_page 一致（< 80 字符视为扫描页）。
    """
    scanned = sampled = 0
    sample_count = min(page_count, 30)
    step = max(1, page_count // sample_count)
    for i in range(0, page_count, step):
        sampled += 1
        if len(doc[i].get_text().strip()) < 80:
            scanned += 1
    is_scanned = sampled > 0 and scanned / sampled >= CHUNK_SCANNED_RATIO
    return CHUNK_PAGES_SCANNED if is_scanned else CHUNK_PAGES_TEXT


@app.on_event("startup")
async def _startup():
    await dispatcher.recover()


@app.post("/jobs", status_code=202)
async def submit_job(file: UploadFile = File(...), split_size: int | None = None):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise HTTPException(400, f"not a valid pdf: {e}")
    try:
        n = doc.page_count
        # 客户端未显式指定 split_size 时，按 PDF 内容自动选择
        if split_size is None or split_size < 1:
            split_size = _auto_split_size(doc, n)
    finally:
        doc.close()
    job_id = uuid.uuid4().hex
    await dispatcher.enqueue(job_id, file.filename or "upload.pdf",
                             split_size, n, data)
    return {"job_id": job_id, "pages": n, "split_size": split_size,
            "num_chunks": math.ceil(n / split_size), "job_id_repeat": job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await dispatcher.job_status(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.model_dump()


@app.get("/stats")
async def stats():
    """系统全貌：多少 job、什么状态、多少分片正在跑 / 等待 / 完成、worker 负载。"""
    return await dispatcher.stats()


@app.get("/jobs/{job_id}/result", response_class=PlainTextResponse)
async def get_result(job_id: str):
    job = await dispatcher.job_status(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status.value != "done":
        raise HTTPException(409, f"job not done: {job.status.value}")
    data = await dispatcher.job_result(job_id)
    if not data:
        raise HTTPException(404, "result missing")
    return PlainTextResponse(data.decode("utf-8"))


@app.post("/internal/claim")
async def claim(backend_id: str | None = None):
    payload = await dispatcher.claim(backend_id)
    if not payload:
        raise HTTPException(204, "no pending chunk")
    return payload


@app.get("/internal/task/{task_id}/pdf")
async def task_pdf(task_id: str):
    """两步式分发第二步：按 claim 返回的 pdf_url 拉取预切 PDF 片段（原始二进制）。"""
    try:
        data = await dispatcher.task_pdf(task_id)
    except FileNotFoundError:
        raise HTTPException(404, f"task not found: {task_id}")
    return Response(content=data, media_type="application/pdf")


# ---------- 取消 / 重跑 ----------

@app.get("/internal/task/{task_id}/state")
async def task_state(task_id: str):
    s = await dispatcher.task_state(task_id)
    if s is None:
        raise HTTPException(404, f"task not found: {task_id}")
    return {"task_id": task_id, "status": s}


@app.post("/internal/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消单个分片：正在解析的会被 worker 立即终止。"""
    ok = await dispatcher.cancel_task(task_id)
    if not ok:
        raise HTTPException(404, f"task not found or already done: {task_id}")
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


@app.post("/internal/task/{task_id}/requeue")
async def requeue_task(task_id: str):
    """重新执行单个分片：清除结果、重新入队分发。"""
    ok = await dispatcher.requeue_task(task_id)
    if not ok:
        raise HTTPException(404, f"task not found: {task_id}")
    return {"ok": True, "task_id": task_id, "status": "pending"}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """取消整个 job：未完成分片全部终止（已完成的结果保留）。"""
    n = await dispatcher.cancel_job(job_id)
    if not n and (await dispatcher.job_status(job_id)) is None:
        raise HTTPException(404, f"job not found: {job_id}")
    return {"ok": True, "job_id": job_id, "cancelled_tasks": n, "status": "cancelled"}


@app.post("/jobs/{job_id}/rerun")
async def rerun_job(job_id: str):
    """整个 job 重跑：全部分片清除结果重新入队。"""
    n = await dispatcher.rerun_job(job_id)
    if not n:
        raise HTTPException(404, f"job not found: {job_id}")
    return {"ok": True, "job_id": job_id, "requeued_tasks": n, "status": "running"}


@app.post("/internal/task_done")
async def task_done(cb: TaskCallback):
    await dispatcher.on_task_done(cb)
    return {"ok": True}


@app.post("/internal/heartbeat")
async def heartbeat(hb: Heartbeat, request: Request):
    caller_ip = request.client.host if request.client else ""
    await dispatcher.register(hb, caller_ip=caller_ip)
    return {"ok": True}
