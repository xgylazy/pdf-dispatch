# -*- coding: utf-8 -*-
"""使用示例：提交 PDF → 轮询状态 → 下载合并后的行记录。"""
import sys
import time
import requests

SCHED = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PDF = sys.argv[2] if len(sys.argv) > 2 else r"D:\project\py_agent\PP-OCRv6\std_docs\GBT35273b.pdf"

with open(PDF, "rb") as f:
    r = requests.post(f"{SCHED}/jobs", files={"file": (PDF, f)},
                      data={"split_size": 10})
r.raise_for_status()
job = r.json()
print("job:", job.get("job_id"), "pages:", job.get("pages"), "job_id_repeat:", job.get("job_id_repeat"))

status = "pending"
while status not in ("done", "failed"):
    time.sleep(2)
    s = requests.get(f"{SCHED}/jobs/{job['job_id']}").json()
    status = s["status"]
    print(f"\r{status:10} chunks_done={s.get('chunks_done', 0)}/{s.get('num_chunks', '?')}", end="", flush=True)
print()

if status == "done":
    out = requests.get(f"{SCHED}/jobs/{job['job_id']}/result")
    out.encoding = "utf-8"
    path = f"{job['job_id']}.result.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(out.text)
    print("saved to", path, "(%d bytes)" % len(out.content))
