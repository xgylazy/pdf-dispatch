import os, json, time

job_prefix = "f03b3a73345d4813bbc715fbc9a18410"

print("=== scheduler.log ALL lines for this job ===")
with open("/opt/pdf-scheduler/data/logs/scheduler.log") as f:
    for line in f:
        if job_prefix[:16] in line or job_prefix in line:
            print(line.strip())

print("\n=== task file history (from stat) ===")
task_dir = "/opt/pdf-scheduler/data/tasks"
for i in range(4):
    path = f"{task_dir}/{job_prefix}_{i}.json"
    st = os.stat(path)
    print(f"chunk_{i}: mtime={time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}.{int((st.st_mtime % 1)*1000):03d}  size={st.st_size}")

result_path = f"/opt/pdf-scheduler/data/results/{job_prefix}.jsonl"
st = os.stat(result_path)
print(f"result:  mtime={time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}.{int((st.st_mtime % 1)*1000):03d}  size={st.st_size}")

print("\n=== per-task records with smallest/largest page ===")
for i in range(4):
    path = f"{task_dir}/{job_prefix}_{i}.json"
    d = json.load(open(path))
    recs = d.get("records", [])
    pages = sorted(set(r.get("page", 0) for r in recs))
    print(f"chunk_{i}: chunk_index={d.get('chunk_index')} pages={d.get('page_start')}-{d.get('page_end')} records={len(recs)} page_range=[{pages[0]}-{pages[-1] if pages else '?'}] backend_id={d.get('backend_id')}")
