# -*- coding: utf-8 -*-
"""pdf_dipatch 自解析引擎。

功能等价于 pdf2tree/app/core/extract.py —— 但不依赖 pdf2tree。
文字页 pymupdf，扫描页 paddle OCR，输出契约与 pdf2tree rows.jsonl 一致。

契约（每条记录）：
  {"t":"page","page":N}
  {"t":"line","page":N,"y":..,"x":..,"yf":..,"text":"..","label":"text","size":..,"bold":..}
  {"t":"table","page":N,"y":..,"yf":..,"html":"<table>..</table>"}
  {"t":"figure","page":N,"y":..,"yf":..,"text":"多行拼接"}
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

import pymupdf

log = logging.getLogger("worker.engine")

# paddle 懒加载
_ocr = None


# ---------------------------------------------------------------------------
# 纯 helpers（与 pdf2tree 行为一致）
# ---------------------------------------------------------------------------

def _cluster_rects(rects: List[pymupdf.Rect], gap: float = 12.0) -> List[pymupdf.Rect]:
    """把间距 <= gap 的矩形合并成大区域（用于图形区检测）。"""
    clusters = []
    for r in rects:
        hit = None
        for i, c in enumerate(clusters):
            if (min(r.x1, c.x1) - max(r.x0, c.x0) >= -gap and
                    min(r.y1, c.y1) - max(r.y0, c.y0) >= -gap):
                hit = i
                break
        if hit is None:
            clusters.append(pymupdf.Rect(r))
        else:
            clusters[hit] = clusters[hit] | r
    return clusters


def _table_to_html(table) -> str:
    """pymupdf Table → HTML，保留 rowspan/colspan。"""
    occupied = set()
    out = ["<table>"]
    for ri, row in enumerate(table.rows):
        tds = []
        for ci, cell in enumerate(row.cells):
            if (ri, ci) in occupied:
                continue
            rs = max(1, getattr(cell, "rowspan", 1))
            cs = max(1, getattr(cell, "colspan", 1))
            if rs > 1 or cs > 1:
                for _dr in range(rs):
                    for _dc in range(cs):
                        occupied.add((ri + _dr, ci + _dc))
            text = (cell.text or "").replace("\n", " ").strip()
            if rs > 1 and cs > 1:
                tds.append(f"<td rowspan='{rs}' colspan='{cs}'>{text}</td>")
            elif rs > 1:
                tds.append(f"<td rowspan='{rs}'>{text}</td>")
            elif cs > 1:
                tds.append(f"<td colspan='{cs}'>{text}</td>")
            else:
                tds.append(f"<td>{text}</td>")
        out.append("<tr>" + "".join(tds) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def _line(pno: int, y: float, x: float, yf: float, text: str,
          label: str = "text", size: Optional[float] = None,
          bold: Optional[bool] = None) -> dict:
    return {"t": "line", "page": pno, "y": float(y), "x": float(x), "yf": float(yf),
            "label": label, "size": size, "bold": bold, "text": text}


def _is_text_page(page: pymupdf.Page, min_chars: int = 80) -> bool:
    """页面分类：文字页 True，扫描页 False。"""
    return len(page.get_text().strip()) >= min_chars


# ---------------------------------------------------------------------------
# 矢量页
# ---------------------------------------------------------------------------

def _vector_page_records(pno: int, page: pymupdf.Page) -> List[dict]:
    """文字页 → 行 + 表格 + 图形区。"""
    page_area = page.rect.get_area()
    ph = page.rect.height or 1.0
    recs: List[dict] = []

    table_boxes: List[pymupdf.Rect] = []
    table_recs: List[dict] = []
    try:
        for t in page.find_tables().tables:
            bbox = pymupdf.Rect(t.bbox)
            table_boxes.append(bbox)
            table_recs.append({"t": "table", "page": pno, "y": float(bbox.y0),
                               "yf": float(bbox.y0 / ph), "html": _table_to_html(t)})
    except Exception:
        pass

    rects = [pymupdf.Rect(d["rect"]) for d in page.get_drawings() if d.get("rect")]
    rects = [r for r in rects
             if min(r.width, r.height) >= 2 and r.get_area() >= page_area * 0.0005]
    clusters = [c for c in _cluster_rects(rects, gap=12.0)
                if c.get_area() >= page_area * 0.001
                and not any(c.intersects(b) for b in table_boxes)]

    figs: List[List[dict]] = [[] for _ in clusters]
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            text = "".join(sp["text"] for sp in ln["spans"]).strip()
            if not text:
                continue
            lbb = ln["bbox"]
            lc = pymupdf.Point((lbb[0] + lbb[2]) / 2, (lbb[1] + lbb[3]) / 2)
            if any(box.contains(lc) for box in table_boxes):
                continue
            hit = None
            for i, c in enumerate(clusters):
                if c.contains(lc):
                    hit = i
                    break
            rec = _line(pno, lbb[1], lbb[0], lbb[1] / ph, text,
                        label="text",
                        size=max(sp["size"] for sp in ln["spans"]),
                        bold=any(bool(sp["flags"] & 16) for sp in ln["spans"]))
            if hit is None:
                recs.append(rec)
            else:
                figs[hit].append(rec)

    for i, c in enumerate(clusters):
        if figs[i]:
            recs.append({"t": "figure", "page": pno, "y": float(c.y0),
                         "yf": float(c.y0 / ph),
                         "text": "\n".join(r["text"] for r in figs[i])})
    recs.extend(table_recs)
    recs.sort(key=lambda r: (r.get("y", 0.0), r.get("x", 0.0)))
    recs.insert(0, {"t": "page", "page": pno})
    return recs


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _get_ocr():
    global _ocr
    if _ocr is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            log.error("paddleocr 未安装")
            _ocr = False
            return None
        cache = os.getenv("PADDLE_PDX_CACHE_HOME", "models")
        off = os.getenv("OCR_MODEL_DIR", "").strip()
        os.environ["PADDLE_PDX_CACHE_HOME"] = off or cache
        try:
            if off:
                args = dict(det_model_dir=str(Path(off) / "PP-OCRv6_medium_det"),
                            rec_model_dir=str(Path(off) / "PP-OCRv6_medium_rec"),
                            use_angle_cls=False, use_gpu=False,
                            enable_mkldnn=True,
                            use_doc_orientation_classify=False,
                            use_doc_unwarping=False,
                            use_textline_orientation=False)
                try:
                    _ocr = PaddleOCR(**args)
                except (TypeError, ValueError):
                    _ocr = PaddleOCR(text_detection_model_name="PP-OCRv6_medium_det",
                                     text_recognition_model_name="PP-OCRv6_medium_rec",
                                     use_angle_cls=False, use_gpu=False,
                                     enable_mkldnn=True,
                                     use_doc_orientation_classify=False,
                                     use_doc_unwarping=False,
                                     use_textline_orientation=False)
            else:
                try:
                    _ocr = PaddleOCR(lang="ch", use_angle_cls=False,
                                     use_gpu=False, enable_mkldnn=True)
                except Exception:
                    _ocr = PaddleOCR()
            log.info("paddle OCR 初始化完成")
        except Exception:
            log.exception("paddle 初始化失败，将仅使用矢量模式")
            _ocr = False
    return _ocr if _ocr else None


def _ocr_page_records(pno: int, page: pymupdf.Page, ocr) -> List[dict]:
    """扫描页 OCR → line 记录（按行输出）。"""
    recs: List[dict] = [{"t": "page", "page": pno}]
    ph = page.rect.height or 1.0
    pw = page.rect.width or 1.0

    pix = page.get_pixmap(dpi=150)
    try:
        res = ocr.predict(pix.tobytes("png"))
    except Exception:
        try:
            res = ocr.ocr(pix.tobytes("png"), cls=False)
        except Exception:
            log.exception("ocr call failed p.%d", pno)
            return recs

    if isinstance(res, dict):
        page_results = res.get("res") or res.get("results") or [res]
    elif isinstance(res, list):
        page_results = res
    else:
        page_results = []

    for pr in (page_results if isinstance(page_results, list) else [page_results]):
        texts = pr.get("rec_texts") or []
        polys = pr.get("rec_polys") or pr.get("dt_polys") or pr.get("rec_boxes") or []
        scores = pr.get("rec_scores") or len(texts) * [None]
        for i, txt in enumerate(texts):
            txt = str(txt).strip()
            if not txt:
                continue
            poly = polys[i] if i < len(polys) else None
            if poly and len(poly) >= 2:
                xs = [p[0] for p in poly]
                ys = [p[1] for p in poly]
                x0 = min(xs) / pix.width * pw
                y0 = min(ys) / pix.height * ph
            else:
                x0 = 0.0
                y0 = 0.0
            recs.append(_line(pno, y0, x0, y0 / ph, txt,
                             label="text",
                             size=None, bold=None))
    return recs


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_pdf(file_bytes: bytes) -> List[dict]:
    """解析 PDF 字节流，返回与 pdf2tree 等价的记录流。"""
    doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    if doc.page_count == 0:
        doc.close()
        return []
    out: List[dict] = []
    try:
        need_ocr = any(not _is_text_page(doc[i]) for i in range(doc.page_count))
        ocr = _get_ocr() if need_ocr else None
        for i in range(doc.page_count):
            page = doc[i]
            if _is_text_page(page):
                out.extend(_vector_page_records(i + 1, page))
            elif ocr is not None:
                try:
                    out.extend(_ocr_page_records(i + 1, page, ocr))
                except Exception:
                    log.exception("ocr parse failed p.%d", i + 1)
            else:
                out.append({"t": "page", "page": i + 1})
                log.info("p.%d 是扫描页但 OCR 不可用，跳过", i + 1)
    finally:
        doc.close()
    return out
