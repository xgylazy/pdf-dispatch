# -*- coding: utf-8 -*-
"""调度核心：内存队列 + claim 驱动分发 + 后端管理 + 结果合并。

完全异步（asyncio）。队列纯内存；持久化走本地文件（见 persistence.py）。

分发模型（claim-driven，无忙等）：
  - 内存池 pending_chunks：deque of (job_id, Chunk)
  - worker 调用 /internal/claim → popleft 一份 → 建 ASSIGNED task → 返回完整 payload
  - worker 完成调 /internal/task_done → 落盘 records → 若 job 全部分片完成则合并
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from typing import List, Optional

from shared.pdfsplit import Chunk, plan_chunks
from shared.protocol import (
    BackendInfo, DispatchPolicy, Heartbeat, JobInfo, JobStatus,
    TaskCallback, TaskInfo, TaskStatus,
)
from scheduler.persistence import FileStore

log = logging.getLogger("dispatcher")


class Dispatcher:
    def __init__(self, store: FileStore,
                 policy: DispatchPolicy = DispatchPolicy.LEAST_LOADED):
        self.store = store
        self.policy = policy
        self._pending: deque = deque()
        self._num_chunks: dict[str, int] = {}
        self._chunks_done: dict[str, int] = {}
        self._pdfs: dict[str, bytes] = {}
        self._backends: dict[str, BackendInfo] = {}

    # ---------- 后端 ----------

    async def register(self, hb: Heartbeat) -> None:
        self._backends[hb.backend_id] = BackendInfo(
            backend_id=hb.backend_id, url=hb.url,
            capacity=hb.capacity, active_tasks=hb.active_tasks,
            pdf_capable=hb.pdf_capable, last_heartbeat=time.time(),
            healthy=True)

    def healthy_backends(self) -> List[BackendInfo]:
        now = time.time()
        out = []
        for b in self._backends.values():
            b.healthy = (now - b.last_heartbeat < 30)
            out.append(b)
        return [b for b in out if b.healthy]

    def _pick_backend(self) -> Optional[BackendInfo]:
        """选一个最闲的健康后端：available = capacity - active_tasks 最大优先。"""
        cands = [b for b in self.healthy_backends()
                 if b.pdf_capable and b.active_tasks < b.capacity]
        if not cands:
            cands = [b for b in self.healthy_backends()
                     if b.active_tasks < b.capacity]
        if not cands:
            return None
        if self.policy == DispatchPolicy.LEAST_LOADED:
            return max(cands, key=lambda c: c.capacity - c.active_tasks)
        return cands[0]

    async def _touch_backend(self, backend_id: str, delta: int) -> None:
        b = self._backends.get(backend_id)
        if b:
            b.active_tasks = max(0, b.active_tasks + delta)

    # ---------- 作业 ----------

    async def enqueue(self, job_id: str, filename: str, split_size: int,
                      total_pages: int, pdf_bytes: bytes) -> JobInfo:
        chunks = plan_chunks(total_pages, split_size)
        job = JobInfo(job_id=job_id, filename=filename, total_pages=total_pages,
                      split_size=split_size, status=JobStatus.RUNNING,
                      num_chunks=len(chunks),
                      created_at=time.time(), updated_at=time.time())
        self.store.save_job(job)
        self.store.save_pdf(job_id, pdf_bytes)
        self._pdfs[job_id] = pdf_bytes
        self._num_chunks[job_id] = len(chunks)
        self._chunks_done[job_id] = 0

        for ck in chunks:
            t = TaskInfo(task_id=f"{job_id}_{ck.index}", job_id=job_id,
                         chunk_index=ck.index, page_start=ck.page_start,
                         page_end=ck.page_end, status=TaskStatus.PENDING,
                         created_at=time.time())
            self.store.save_task(t)
            self._pending.append((job_id, ck))
        return job

    async def recover(self) -> None:
        """启动时把未完成作业的分片重新压回 pending 池。"""
        for job_id in self.store.recover():
            job = self.store.load_job(job_id)
            if not job:
                continue
            try:
                pdf = self.store.load_pdf(job_id)
                self._pdfs[job_id] = pdf
            except FileNotFoundError:
                continue
            self._num_chunks[job_id] = job.num_chunks
            self._chunks_done[job_id] = 0
            for td in self.store.pending_tasks_of(job_id):
                self._pending.append(
                    (job_id, Chunk(index=td["chunk_index"],
                                   page_start=td["page_start"],
                                   page_end=td["page_end"],
                                   page_count=td["page_end"] - td["page_start"] + 1)))
            log.info("recovered job %s", job_id)

    # ---------- 分发：worker claim ----------

    async def claim(self, backend_id: str | None = None) -> Optional[dict]:
        if not self._pending:
            return None
        job_id, ck = self._pending.popleft()
        be = backend_id or (self._pick_backend().backend_id
                            if self._pick_backend() else "local")
        await self.store.update_task(
            f"{job_id}_{ck.index}", **{"status": TaskStatus.ASSIGNED,
                                       "backend_id": be})
        await self._touch_backend(be, +1)
        return await self._payload(job_id, ck)

    async def _payload(self, job_id: str, ck: Chunk) -> dict:
        job = self.store.load_job(job_id)
        pdf = self._pdfs.get(job_id)
        if pdf is None:
            pdf = self.store.load_pdf(job_id)
            self._pdfs[job_id] = pdf
        return {
            "task_id": f"{job_id}_{ck.index}",
            "job_id": job_id,
            "filename": job.filename if job else "",
            "chunk_index": ck.index,
            "page_start": ck.page_start, "page_end": ck.page_end,
            "total_pages": job.total_pages if job else 0,
            "pdf_bytes": base64.b64encode(pdf).decode("ascii"),
        }

    # ---------- 回调 ----------

    async def on_task_done(self, cb: TaskCallback) -> None:
        self.store.save_task_result(
            cb.task_id, ok=cb.ok, records=cb.records,
            text_concat=cb.text_concat, error=cb.error, parse_ms=cb.parse_ms)
        await self._touch_backend(cb.backend_id or "", -1)
        done = self._chunks_done.get(cb.job_id, 0) + (1 if cb.ok else 0)
        self._chunks_done[cb.job_id] = done
        self.store.update_job(cb.job_id, chunks_done=done)
        job = self.store.load_job(cb.job_id)
        if job and done >= job.num_chunks:
            await self.store.update_job(cb.job_id, status=JobStatus.MERGING)
            await self._merge(cb.job_id)

    async def _merge(self, job_id: str) -> None:
        records: list[dict] = []
        for t in self.store.tasks_of(job_id):
            r = self.store.task_result(t.task_id)
            if r and r.get("ok") and r.get("records"):
                records.extend(r["records"])
        data = "\n".join(json.dumps(x, ensure_ascii=False)
                         for x in records).encode("utf-8")
        self.store.save_result(job_id, data)
        self.store.update_job(job_id, status=JobStatus.DONE)

    # ---------- 查询 ----------

    async def job_status(self, job_id: str) -> Optional[JobInfo]:
        return self.store.load_job(job_id)

    async def job_result(self, job_id: str) -> Optional[bytes]:
        try:
            return self.store.load_result(job_id)
        except FileNotFoundError:
            return None
