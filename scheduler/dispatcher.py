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
import httpx
import json
import logging
import time
from collections import deque
from typing import List, Optional

import pymupdf

from shared.pdfsplit import Chunk, extract_page_range, plan_chunks
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
        self._aborted: set[str] = set()   # 被取消的 task_id（worker 解析时会轮询到）
        self._backends: dict[str, BackendInfo] = {}

    # ---------- 后端 ----------

    async def register(self, hb: Heartbeat, caller_ip: str = "") -> None:
        # backend_id 由 worker 用 UUID 自生成，保证全局唯一；
        # 拼上 caller_ip 是为了兜底手工部署时两个 worker 配置了相同 BACKEND_ID 的情况。
        key = f"{hb.backend_id}@{caller_ip}" if caller_ip and caller_ip not in hb.backend_id else hb.backend_id
        self._backends[key] = BackendInfo(
            backend_id=hb.backend_id, url=hb.url, ip=hb.ip, pid=hb.pid,
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
        # 物理预切：每个 chunk 裁成独立小 PDF 落盘，claim 时只下发该片段
        # （避免每次 claim 重复传输整本 PDF）
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            for ck in chunks:
                piece = pymupdf.open()
                piece.insert_pdf(doc, from_page=ck.page_start - 1,
                                 to_page=ck.page_end - 1)
                self.store.save_pdf_piece(job_id, ck.index, piece.tobytes())
                piece.close()
        finally:
            doc.close()
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
            if job.status == JobStatus.CANCELLED:
                continue    # 已取消的 job 不复活
            self._num_chunks[job_id] = job.num_chunks
            # 启动时从磁盘统计已完成的 chunk，避免重启后计数器归零
            done = sum(1 for t in self.store.tasks_of(job_id)
                       if t.status == TaskStatus.DONE)
            self._chunks_done[job_id] = done
            for td in self.store.pending_tasks_of(job_id):
                self._pending.append(
                    (job_id, Chunk(index=td["chunk_index"],
                                   page_start=td["page_start"],
                                   page_end=td["page_end"],
                                   page_count=td["page_end"] - td["page_start"] + 1)))
            await self.store.update_job(job_id, chunks_done=done)
            if done >= job.num_chunks:
                await self.store.update_job(job_id, status=JobStatus.MERGING)
                await self._merge(job_id)
                log.info("recovered job %s already complete, merged at startup", job_id)
            log.info("recovered job %s (%d/%d done)", job_id, done, job.num_chunks)

    # ---------- 分发：worker claim ----------

    async def claim(self, backend_id: str | None = None) -> Optional[dict]:
        while self._pending:
            job_id, ck = self._pending.popleft()
            task_id = f"{job_id}_{ck.index}"
            if task_id in self._aborted:
                continue    # 已取消的任务直接丢弃（磁盘状态已是 CANCELLED）
            be = backend_id or (self._pick_backend().backend_id
                                if self._pick_backend() else "local")
            await self.store.update_task(
                task_id, **{"status": TaskStatus.ASSIGNED, "backend_id": be})
            await self._touch_backend(be, +1)
            return await self._payload(job_id, ck)
        return None

    async def _payload(self, job_id: str, ck: Chunk) -> dict:
        job = self.store.load_job(job_id)
        task_id = f"{job_id}_{ck.index}"
        return {
            "task_id": task_id,
            "job_id": job_id,
            "filename": job.filename if job else "",
            "chunk_index": ck.index,
            "page_start": ck.page_start, "page_end": ck.page_end,
            "total_pages": job.total_pages if job else 0,
            # 两步式：claim 只发元数据，PDF 二进制由 worker 按 pdf_url 原样拉取
            "pdf_url": f"/internal/task/{task_id}/pdf",
        }

    async def task_pdf(self, task_id: str) -> bytes:
        """返回某个 task 对应的预切 PDF 片段（原始二进制）。

        分片文件缺失时（老任务/异常恢复）从整本 PDF 现场裁页兜底。
        """
        t = self.store.load_task(task_id)
        if not t:
            raise FileNotFoundError(task_id)
        piece = self.store.load_pdf_piece(t.job_id, t.chunk_index)
        if piece is not None:
            return piece
        pdf = self.store.load_pdf(t.job_id)
        return extract_page_range(pdf, t.page_start, t.page_end)

    # ---------- 回调 ----------

    def _find_backend(self, backend_id: str) -> Optional[BackendInfo]:
        b = self._backends.get(backend_id)
        if b:
            return b
        bid = backend_id.split("@")[0]
        return next((v for k, v in self._backends.items()
                     if k.split("@")[0] == bid), None)

    async def _push_abort(self, backend_id: str, task_id: str) -> None:
        """尽力而为地通知 worker 立刻终止该任务；推送失败也不影响正确性
        （结果在 task_done 阶段仍会被丢弃）。"""
        b = self._find_backend(backend_id) if backend_id else None
        if not b or not b.url:
            return
        try:
            async with httpx.AsyncClient(timeout=3) as cli:
                await cli.post(f"{b.url}/abort/{task_id}")
        except Exception:
            pass

    async def task_state(self, task_id: str) -> Optional[str]:
        t = self.store.load_task(task_id)
        return t.status.value if t else None

    async def cancel_task(self, task_id: str) -> bool:
        t = self.store.load_task(task_id)
        if not t or t.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            return False
        self._aborted.add(task_id)
        await self.store.update_task(task_id, status=TaskStatus.CANCELLED)
        await self._push_abort(t.backend_id or "", task_id)
        return True

    async def requeue_task(self, task_id: str) -> bool:
        """重新执行某个分片：清除结果、重新入队（预切 PDF 片会重发给 worker）。"""
        t = self.store.load_task(task_id)
        if not t:
            return False
        self._aborted.discard(task_id)
        r = self.store.task_result(task_id)
        if r and r.get("ok"):
            self._chunks_done[t.job_id] = max(0, self._chunks_done.get(t.job_id, 0) - 1)
            await self.store.update_job(t.job_id, chunks_done=self._chunks_done[t.job_id])
        self.store.clear_task_result(task_id)
        await self.store.update_task(task_id, status=TaskStatus.PENDING)
        job = self.store.load_job(t.job_id)
        if job and job.status == JobStatus.CANCELLED:
            await self.store.update_job(t.job_id, status=JobStatus.RUNNING)
        self._pending.append((t.job_id, Chunk(index=t.chunk_index,
                                              page_start=t.page_start,
                                              page_end=t.page_end,
                                              page_count=t.page_end - t.page_start + 1)))
        return True

    async def cancel_job(self, job_id: str) -> int:
        """取消整个 job：未完成的分片全部标记 CANCELLED 并推送终止指令。"""
        job = self.store.load_job(job_id)
        if not job:
            return 0
        n = 0
        for t in self.store.tasks_of(job_id):
            if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                continue
            self._aborted.add(t.task_id)
            await self.store.update_task(t.task_id, status=TaskStatus.CANCELLED)
            await self._push_abort(t.backend_id or "", t.task_id)
            n += 1
        self._pending = deque(x for x in self._pending if x[0] != job_id)
        await self.store.update_job(job_id, status=JobStatus.CANCELLED)
        return n

    async def rerun_job(self, job_id: str) -> int:
        """整个 job 重跑：清结果、全部重新入队。"""
        if not self.store.load_job(job_id):
            return 0
        n = 0
        for t in self.store.tasks_of(job_id):
            self._aborted.discard(t.task_id)
            self.store.clear_task_result(t.task_id)
            await self.store.update_task(t.task_id, status=TaskStatus.PENDING)
            self._pending.append((job_id, Chunk(index=t.chunk_index,
                                                page_start=t.page_start,
                                                page_end=t.page_end,
                                                page_count=t.page_end - t.page_start + 1)))
            n += 1
        self._chunks_done[job_id] = 0
        await self.store.update_job(job_id, status=JobStatus.RUNNING, chunks_done=0)
        return n

    async def on_task_done(self, cb: TaskCallback) -> None:
        # 已取消/删除的任务：结果直接丢弃
        if cb.task_id in self._aborted:
            await self._touch_backend(cb.backend_id or "", -1)
            return
        t = self.store.load_task(cb.task_id)
        if t is not None and t.status == TaskStatus.CANCELLED:
            await self._touch_backend(cb.backend_id or "", -1)
            return
        self.store.save_task_result(
            cb.task_id, ok=cb.ok, records=cb.records,
            text_concat=cb.text_concat, error=cb.error, parse_ms=cb.parse_ms)
        await self._touch_backend(cb.backend_id or "", -1)
        self._chunks_done[cb.job_id] = self._chunks_done.get(cb.job_id, 0) + (1 if cb.ok else 0)
        await self.store.update_job(cb.job_id, chunks_done=self._chunks_done[cb.job_id])
        # 扫盘判断：4 个 task 全部 ok=True 后才触发合并（而非信任内存 counter）
        job = self.store.load_job(cb.job_id)
        if not job:
            return
        tasks = self.store.tasks_of(cb.job_id)
        all_ok = (len(tasks) == job.num_chunks
                  and all(((r := self.store.task_result(t.task_id)) and r.get("ok"))
                          for t in tasks))
        if all_ok:
            await self.store.update_job(cb.job_id, status=JobStatus.MERGING)
            await self._merge(cb.job_id)

    async def _merge(self, job_id: str) -> None:
        # 不从内存 counter 做判断——直接扫盘：只有当 4 个 task 全部 DONE/ok=True
        # 时才真正合并，避免 await 交错导致部分回调未写回就触发 merge。
        records: list[dict] = []
        tasks = self.store.tasks_of(job_id)
        for t in tasks:
            r = self.store.task_result(t.task_id)
            if r and r.get("ok") and r.get("records"):
                records.extend(r["records"])
        # 防御：如果 scan 到的 ok=True task 数不够，说明还有 worker 没回写，
        # 直接 return 等下次 job 状态刷新（比如 recover() 或下次 on_task_done）。
        ok_count = sum(1 for t in tasks
                       if (r := self.store.task_result(t.task_id)) and r.get("ok"))
        job = self.store.load_job(job_id)
        expected = job.num_chunks if job else 0
        if ok_count < expected:
            log.warning("merge skipped: only %d/%d tasks done on disk for %s — retry later",
                         ok_count, expected, job_id)
            return
        data = "\n".join(json.dumps(x, ensure_ascii=False)
                         for x in records).encode("utf-8")
        self.store.save_result(job_id, data)
        await self.store.update_job(job_id, status=JobStatus.DONE)

    # ---------- 查询 ----------

    async def stats(self) -> dict:
        jobs = self.store.list_jobs()
        by_status: dict[str, int] = {}
        for j in jobs:
            by_status[j.status.value] = by_status.get(j.status.value, 0) + 1
        # 分片维度：pending = 在内存队列里等 claim；done = 已完成回调；
        # executing = 已 claim 出队但未回调（= pending_least_loaded 之外的已分配）。
        # 用总 - pending - done 近似（内存池 + 已持久化_DONE 两处之和守恒）。
        total_chunks = sum(self._num_chunks.values())
        pending = len(self._pending)
        done = sum(self._chunks_done.values())
        executing = max(0, total_chunks - pending - done)
        backends = []
        for b in self.healthy_backends():
            backends.append({
                "backend_id": b.backend_id,
                "ip": b.ip,
                "pid": b.pid,
                "active_tasks": b.active_tasks,
                "capacity": b.capacity,
                "healthy": b.healthy,
            })
        return {
            "jobs": {
                "total": len(jobs),
                "by_status": by_status,
                "items": [
                    {
                        "job_id": j.job_id,
                        "status": j.status.value,
                        "num_chunks": j.num_chunks,
                        "chunks_done": j.chunks_done,
                        "filename": j.filename,
                    }
                    for j in sorted(jobs, key=lambda x: x.created_at, reverse=True)
                ],
            },
            "chunks": {
                "total": total_chunks,
                "pending": pending,
                "executing": executing,
                "done": done,
            },
            "backends": backends,
        }

    async def job_status(self, job_id: str) -> Optional[JobInfo]:
        return self.store.load_job(job_id)

    async def job_result(self, job_id: str) -> Optional[bytes]:
        try:
            return self.store.load_result(job_id)
        except FileNotFoundError:
            return None
