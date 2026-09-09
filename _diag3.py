import sys, json, os
sys.path.insert(0, "/opt/pdf-scheduler")
from scheduler.persistence import FileStore
s = FileStore("/opt/pdf-scheduler/data")
job_id = "f03b3a73345d4813bbc715fbc9a18410"

# Check the actual result file
result_path = f"/opt/pdf-scheduler/data/results/{job_id}.jsonl"
with open(result_path) as f:
    lines = f.readlines()
print(f"File: {len(lines)} lines, {os.path.getsize(result_path)} bytes")

# Count pages
pages = {}
for line in lines:
    try:
        d = json.loads(line)
        p = d.get("page", "?")
        pages[p] = pages.get(p, 0) + 1
    except:
        pass

print(f"Distinct pages: {sorted([x for x in pages if isinstance(x,int)])}")

# Check last page
last_page_line = None
for line in lines:
    try:
        d = json.loads(line)
        if d.get("t") == "page":
            last_page_line = d
    except:
        pass
print(f"Last page marker: {last_page_line}")

# If only page 10 appears at end, check merge code execution
print()
print("=== Manually running _merge logic ===")
tasks = s.tasks_of(job_id)
records = []
for t in tasks:
    r = s.task_result(t.task_id)
    if r and r.get("ok") and r.get("records"):
        records.extend(r["records"])
print(f"Manual merge would produce {len(records)} records from {len(tasks)} tasks")

# Check actual result records count
result_records = sum(1 for line in lines if '"t": "line"' in line or '"t":"line"' in line)
print(f"Actual result file line-count: {len(lines)}, records with t=line: {result_records}")
