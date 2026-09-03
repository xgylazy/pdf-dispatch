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

import uuid

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from scheduler.dispatcher import Dispatcher
from scheduler.persistence import FileStore
from shared.protocol import Heartbeat, TaskCallback

store = FileStore()
dispatcher = Dispatcher(store)
app = FastAPI(title="pdf-dispatch scheduler")


@app.on_event("startup")
async def _startup():
    await dispatcher.recover()


@app.post("/jobs", status_code=202)
async def submit_job(file: UploadFile = File(...), split_size: int = 10):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        n = pymupdf.open(stream=data, filetype="pdf").page_count
    except Exception as e:
        raise HTTPException(400, f"not a valid pdf: {e}")
    job_id = uuid.uuid4().hex
    await dispatcher.enqueue(job_id, file.filename or "upload.pdf",
                             split_size, n, data)
    return {"job_id": job_id, "pages": n, "job_id_repeat": job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await dispatcher.job_status(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.model_dump()


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


@app.post("/internal/task_done")
async def task_done(cb: TaskCallback):
    await dispatcher.on_task_done(cb)
    return {"ok": True}


@app.post("/internal/heartbeat")
async def heartbeat(hb: Heartbeat):
    await dispatcher.register(hb)
    return {"ok": True}
