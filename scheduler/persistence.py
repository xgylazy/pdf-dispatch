# -*- coding: utf-8 -*-
"""文件持久化层——替代 MongoDB。

目录结构（调度中心本地）：
  data/
    pdfs/<job_id>.pdf            原始 PDF
    results/<job_id>.jsonl       合并后的行记录
    jobs/<job_id>.json           作业状态（JobInfo）
    tasks/<task_id>.json         分片任务状态（TaskInfo + records + ok）

写入策略：先写 .tmp → os.replace 原子替换（防写到一半崩溃拿半截文件）。
恢复策略：启动时扫 data/tasks/*.json，把 ok=false 或 status∈{PENDING,ASSIGNED,RUNNING} 的 job 重新入队。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator, List

from shared.protocol import JobInfo, JobStatus, TaskInfo, TaskStatus


class FileStore:
    def __init__(self, data_dir: str = "data"):
        self.root = Path(data_dir).resolve()
        self.pdf_dir = self.root / "pdfs"
        self.result_dir = self.root / "results"
        self.job_dir = self.root / "jobs"
        self.task_dir = self.root / "tasks"
        for d in (self.pdf_dir, self.result_dir, self.job_dir, self.task_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- pdf / results（二进制 / 文本大文件） ----------

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """先写 .tmp + os.replace → 崩溃不会留半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            # 清理残留 .tmp
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_pdf(self, job_id: str, data: bytes) -> None:
        self._atomic_write(self.pdf_dir / f"{job_id}.pdf", data)

    def load_pdf(self, job_id: str) -> bytes:
        return (self.pdf_dir / f"{job_id}.pdf").read_bytes()

    def save_result(self, job_id: str, data: bytes) -> None:
        self._atomic_write(self.result_dir / f"{job_id}.jsonl", data)

    def load_result(self, job_id: str) -> bytes:
        return (self.result_dir / f"{job_id}.jsonl").read_bytes()

    # ---------- jobs（小 JSON） ----------

    def save_job(self, job: JobInfo) -> None:
        p = self.job_dir / f"{job.job_id}.json"
        self._atomic_write(p, json.dumps(job.model_dump(),
                                        ensure_ascii=False, indent=2).encode("utf-8"))

    def load_job(self, job_id: str) -> JobInfo | None:
        p = self.job_dir / f"{job_id}.json"
        return JobInfo(**json.loads(p.read_text(encoding="utf-8"))) if p.is_file() else None

    async def update_job(self, job_id: str, **kw) -> None:
        job = self.load_job(job_id)
        if not job:
            return
        for k, v in kw.items():
            setattr(job, k, v)
        job.updated_at = time.time()
        self.save_job(job)

    # ---------- tasks（小 JSON，含 records） ----------

    @staticmethod
    def _task_path(task_dir: Path, task_id: str) -> Path:
        return task_dir / f"{task_id}.json"

    def save_task(self, t: TaskInfo) -> None:
        p = self._task_path(self.task_dir, t.task_id)
        self._atomic_write(p, json.dumps(t.model_dump(),
                                        ensure_ascii=False, indent=2).encode("utf-8"))

    async def update_task(self, task_id: str, **kw) -> None:
        t = self.load_task(task_id)
        if not t:
            return
        for k, v in kw.items():
            setattr(t, k, v)
        self.save_task(t)

    def save_task_result(self, task_id: str, *, ok: bool, records: list,
                         text_concat: str, error: str | None, parse_ms: int) -> None:
        p = self._task_path(self.task_dir, task_id)
        obj = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        obj.update(ok=ok, records=records, text_concat=text_concat,
                   error=error, parse_ms=parse_ms, updated_at=time.time(),
                   status=TaskStatus.DONE if ok else TaskStatus.FAILED)
        self._atomic_write(p, json.dumps(obj, ensure_ascii=False,
                                        indent=2).encode("utf-8"))

    def load_task(self, task_id: str) -> TaskInfo | None:
        p = self._task_path(self.task_dir, task_id)
        return TaskInfo(**json.loads(p.read_text(encoding="utf-8"))) if p.is_file() else None

    def task_result(self, task_id: str) -> dict | None:
        p = self._task_path(self.task_dir, task_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def tasks_of(self, job_id: str) -> List[TaskInfo]:
        out: List[TaskInfo] = []
        for p in self.task_dir.glob(f"{job_id}_*.json"):
            try:
                out.append(TaskInfo(**json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                continue
        out.sort(key=lambda t: t.chunk_index)
        return out

    # ---------- 崩溃恢复 ----------

    def recover(self) -> Iterator[str]:
        """扫描所有未完成的任务，产出所属的 job_id（去重）。"""
        seen: set[str] = set()
        for p in self.task_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = d.get("status")
            if status in (TaskStatus.PENDING, TaskStatus.ASSIGNED,
                          TaskStatus.RUNNING):
                job_id = d.get("job_id")
                if job_id and job_id not in seen:
                    seen.add(job_id)
                    yield job_id

    def pending_tasks_of(self, job_id: str) -> List[dict]:
        """返回 job_id 下所有未完成的 task dict（含 task_id / chunk_index / page_*）."""
        out: List[dict] = []
        for p in self.task_dir.glob(f"{job_id}_*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("status") in (TaskStatus.PENDING, TaskStatus.ASSIGNED,
                                   TaskStatus.RUNNING):
                out.append(d)
        out.sort(key=lambda x: x.get("chunk_index", 0))
        return out
