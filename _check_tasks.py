import json,glob,os
task_dir="/opt/pdf-scheduler/data/tasks"
prefix="f03b3a73345d4813bbc715fbc9a18410"
for i in range(4):
    p=os.path.join(task_dir, f"{prefix}_{i}.json")
    d=json.load(open(p))
    print(f"chunk_{i}: ok={d.get('ok')} status={d.get('status')} records={len(d.get('records',[]))}")
