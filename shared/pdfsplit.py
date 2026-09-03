# -*- coding: utf-8 -*-
"""PDF 分页切片 / 合并工具 —— 复用 pdf2tree 同一套 pymupdf 基础。

调度中心整存 PDF（二进制入 Mongo GridFS），下发时只传 {page_start, page_end}
让 worker 自己裁页，避免调度中心做字节拷贝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Chunk:
    index: int
    page_start: int     # 1-based 含
    page_end: int       # 1-based 含
    page_count: int


def plan_chunks(total_pages: int, split_size: int) -> List[Chunk]:
    """按 split_size 页一份切分，返回 Chunk 列表。"""
    chunks: List[Chunk] = []
    idx = 0
    p = 1
    while p <= total_pages:
        end = min(p + split_size - 1, total_pages)
        chunks.append(Chunk(index=idx, page_start=p, page_end=end,
                            page_count=end - p + 1))
        idx += 1
        p = end + 1
    return chunks


def extract_page_range(pdf_bytes: bytes, page_start: int, page_end: int) -> bytes:
    """把完整 PDF 裁出 [page_start, page_end]（1-based）并返回独立 PDF 字节。

    用 pymupdf 做裁页：新文档只 select 指定页号，可被 worker 原样送入
    pdf2tree.extract.extract_records。
    """
    import pymupdf
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    n = src.page_count
    s = max(1, page_start) - 1          # 转 0-based
    e = min(n, page_end) - 1
    if s > e:
        src.close()
        return b""
    out = pymupdf.open()
    # pymupdf insert_pdf: (from_doc, from_page, to_page) 0-based 含
    out.insert_pdf(src, from_page=s, to_page=e)
    data = out.tobytes()
    out.close()
    src.close()
    return data
