# -*- coding: utf-8 -*-
"""调度中心与 worker 之间的协议（Pydantic 模型）。"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"          # 进入队列，等切片
    DISPATCHING = "dispatching"  # 正在切分分发给后端
    RUNNING = "running"          # 局部完成中
    MERGING = "merging"          # 所有分片完成，合并中
    DONE = "done"                # 全部完成可取结果
    FAILED = "failed"            # 后端失败超限，作业失败


class TaskStatus(str, Enum):
    PENDING = "pending"      # 调度中心已下发，worker 未拉取
    ASSIGNED = "assigned"    # worker 已取走
    RUNNING = "running"      # worker 正在解析
    DONE = "done"            # worker 完成，已回调
    FAILED = "failed"        # 解析失败或超时


# ---------- 作业 ----------

class SubmitJobRequest(BaseModel):
    """客户端提交 PDF 时的元数据（文件通过 multipart 上传）。"""
    callback_url: Optional[str] = None            # 可选：完成后 webhook
    split_size: int = Field(10, ge=1, le=200)    # 每分片多少页
    priority: int = Field(0, ge=0, le=9)          # 调度优先级（高优先先出队）


class JobInfo(BaseModel):
    job_id: str
    filename: str
    total_pages: int
    split_size: int
    status: JobStatus
    num_chunks: int = 0
    chunks_done: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    error: Optional[str] = None


# ---------- 任务分片 ----------

class TaskInfo(BaseModel):
    task_id: str           # {job_id}_{chunk_index}
    job_id: str
    chunk_index: int
    page_start: int        # 1-based 含
    page_end: int          # 1-based 含
    status: TaskStatus
    backend_id: Optional[str] = None
    created_at: float = 0.0


class TaskPullResponse(BaseModel):
    """worker 拉取任务时分发的数据：pdf 字节 + 页面范围。"""
    task_id: str
    job_id: str
    filename: str
    chunk_index: int
    page_start: int           # 1-based
    page_end: int             # 1-based
    total_pages: int          # 原始 pdf 总页数（worker 无需回查）
    pdf_bytes: str            # base64 编码的完整 PDF（worker 自己裁页）


class TaskCallback(BaseModel):
    """worker 完成解析后主动回调调度中心。"""
    task_id: str
    job_id: str
    chunk_index: int
    page_start: int
    page_end: int
    ok: bool
    """解析产物，每行一条记录（pdf2tree 的统一行契约）"""
    records: List[dict] = []   # {t:line/table/page, page, y, x, yf, text, ...}
    text_concat: str = ""      # 可选：拼接全文（下游快速索引）
    error: Optional[str] = None
    parse_ms: int = 0          # 本分片耗时


# ---------- 后端 ----------

class Heartbeat(BaseModel):
    backend_id: str
    url: str                       # worker 的对外地址（http://ip:port）
    capacity: int = 4              # 并发槽位数
    active_tasks: int = 0          # 当前正在跑的任务数
    pdf_capable: bool = True       # 是否部署了 paddleocr（扫描页需要）


class BackendInfo(BaseModel):
    backend_id: str
    url: str
    capacity: int = 4
    active_tasks: int = 0
    pdf_capable: bool = True
    last_heartbeat: float = 0.0
    healthy: bool = True


class DispatchPolicy(str, Enum):
    """分片下发策略。"""
    ROUND_ROBIN = "round_robin"        # 轮流
    LEAST_LOADED = "least_loaded"      # 选最空闲
    OCR_AWARE = "ocr_aware"            # 扫描页优先发给 pdf_capable 的后端
