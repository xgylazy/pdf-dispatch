import sys, json, os
sys.path.insert(0, "/opt/pdf-scheduler")
from scheduler.persistence import FileStore
s = FileStore("/opt/pdf-scheduler/data")
for i in range(4):
    tid = f"f03b3a73345d4813bbc715fbc9a18410_{i}"
    r = s.task_result(tid)
    if r:
        recs = r.get("records", [])
        print(f"chunk_{i}: ok={r.get('ok')} records_len={len(recs)} page_range=({r.get('page_start')}-{r.get('page_end')}) error={r.get('error')}")
    else:
        print(f"chunk_{i}: task_result=None")
