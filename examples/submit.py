# -*- coding: utf-8 -*-
"""使用示例：提交 PDF → 轮询状态 → 下载合并后的行记录。"""
import sys
import time
import os
import requests

SCHED = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:28765"
PDF = sys.argv[2] if len(sys.argv) > 2 else r"D:\project\py_agent\PP-OCRv6\std_docs\GBT35273b.pdf"
PDF = PDF.strip("\'\"")

if not os.path.isfile(PDF):
    print(f"[ERROR] 文件不存在: {PDF}")
    print("提示：路径包含空格时请用双引号包裹，例如：")
    print('  python examples/submit.py http://host:28765 "D:/path/to/我的 文件.pdf"')
    sys.exit(1)

t0 = time.time()

with open(PDF, "rb") as f:
    # split_size 由调度中心按 PDF 内容自动判定（扫描件/文字页规则见 build.conf）
    r = requests.post(f"{SCHED}/jobs", files={"file": (PDF, f)})
r.raise_for_status()
job = r.json()
print("job:", job.get("job_id"), "pages:", job.get("pages"),
      "split:", job.get("split_size"), "chunks:", job.get("num_chunks"))

status = "pending"
while status not in ("done", "failed"):
    time.sleep(2)
    s = requests.get(f"{SCHED}/jobs/{job['job_id']}").json()
    status = s["status"]
    print(f"\r{status:10} chunks_done={s.get('chunks_done', 0)}/{s.get('num_chunks', '?')}", end="", flush=True)
print()

elapsed = time.time() - t0

if status == "done":
    out = requests.get(f"{SCHED}/jobs/{job['job_id']}/result")
    out.encoding = "utf-8"
    path = f"{job['job_id']}.result.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(out.text)
    print("saved to", path, "(%d bytes)" % len(out.content))
print(f"总耗时 {elapsed:.2f}s")