import sys, json, os, time
sys.path.insert(0, "/opt/pdf-scheduler")
from scheduler.persistence import FileStore

s = FileStore("/opt/pdf-scheduler/data")
job_id = "f03b3a73345d4813bbc715fbc9a18410"
job = s.load_job(job_id)
print(f"job: status={job.status} num_chunks={job.num_chunks} chunks_done={job.chunks_done}")
print(f"created_at={job.ctime}" if hasattr(job,'ctime') else "")

tasks = s.tasks_of(job_id)
print(f"\n=== {len(tasks)} tasks on disk ===")
for t in tasks:
    r = s.task_result(t.task_id)
    if r:
        recs = r.get("records", [])
        pages = sorted(set(rec.get("page", 0) for rec in recs))
        # Check raw file mtime
        task_path = f"/opt/pdf-scheduler/data/tasks/{t.task_id}.json"
        mtime = os.path.getmtime(task_path)
        print(f"  {t.task_id}: pages {t.page_start}-{t.page_end}, records={len(recs)}, ok={r.get('ok')}, mtime={time.strftime('%H:%M:%S', time.localtime(mtime))}")
    else:
        print(f"  {t.task_id}: no data")

# Check result file
result_path = f"/opt/pdf-scheduler/data/results/{job_id}.jsonl"
if os.path.exists(result_path):
    mtime = os.path.getmtime(result_path)
    with open(result_path) as f:
        lines = f.readlines()
    pages = set()
    for line in lines:
        try:
            d = json.loads(line)
            if "page" in d:
                pages.add(d["page"])
        except:
            pass
    print(f"\n=== result.jsonl ===")
    print(f"  lines={len(lines)} mtime={time.strftime('%H:%M:%S', time.localtime(mtime))}")
    print(f"  distinct pages: {sorted(pages)}")
else:
    print("\nresult.jsonl: NOT FOUND")

# Check scheduler.log for task_done timestamps
print(f"\n=== scheduler.log timeline for {job_id} ===")
with open("/opt/pdf-scheduler/data/logs/scheduler.log") as f:
    for line in f:
        if job_id[:12] in line:
            ts = line.split(" - ")[0] if " - " in line else "?"
            # Show relevant lines
            if any(x in line for x in ["task_done", "POST /jobs ", "claim"]):
                print(f"  {ts}: {line.strip()[:150]}")
