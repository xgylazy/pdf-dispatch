#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""唤醒 worker 机器（虚拟机重启后手动拉起，不会清任务状态）。

用法（项目根目录）：
    python scripts/start.py                                  # 唤醒 servers.txt 里全部 worker
    python scripts/start.py root@192.192.98.80               # 只唤醒指定的
    python scripts/start.py root@192.192.98.80 root@192.192.98.81

说明：
  * 已在运行的机器自动跳过，不会重复拉起
  * 只跑 start.sh，不解压、不清 data/，任务状态完好无损
  * scheduler 地址自动从 servers.txt 读取并显式注入（不依赖包内默认值）
  * scheduler 本身不在本脚本职责内：机器重启后需手动拉起
    （ssh root@<ip> "cd /opt/pdf-scheduler && setsid ./start.sh >> data/logs/scheduler.log 2>&1 < /dev/null &"）
"""
from __future__ import annotations

import concurrent.futures
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402  复用 ssh 选项


def scheduler_url_from(servers_path: Path) -> str:
    for line in servers_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.endswith("app=scheduler"):
            return f"http://{line.split()[0].split('@')[-1]}:28765"
    print(f"[ERROR] {servers_path} 里没有 app=scheduler 的行")
    sys.exit(2)


def worker_hosts_from(servers_path: Path) -> list[str]:
    hosts = []
    for line in servers_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.endswith("app=worker"):
            hosts.append(line.split()[0])
    return list(dict.fromkeys(hosts))


def build_remote_script(scheduler_url: str) -> str:
    return f"""if pgrep -f "worker.main" >/dev/null 2>&1; then
  echo "[skip] worker已在运行"
  exit 0
fi
cd /opt/pdf-worker || exit 1
mkdir -p data/logs
export SCHEDULER_URL={scheduler_url}
setsid nohup ./start.sh >> data/logs/worker.log 2>&1 < /dev/null &
disown
sleep 3
if pgrep -f "worker.main" >/dev/null 2>&1; then
  echo "[started] pid=$(pgrep -f worker.main | head -1)"
else
  echo "[FAILED] 启动后 3 秒内未见到进程，最近日志："
  tail -n 10 data/logs/worker.log 2>/dev/null
  exit 1
fi
"""


def start_one(userhost: str, scheduler_url: str, opts: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["ssh", *opts, userhost, "bash -s"],
            input=build_remote_script(scheduler_url).encode("utf-8"),
            capture_output=True, timeout=60)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if r.returncode != 0:
            return False, f"[FAILED]  {userhost}: {(out + err).strip()[-400:]}"
        return True, f"{out}\n"
    except subprocess.TimeoutExpired:
        return False, f"[FAILED]  {userhost}: ssh 超时"
    except Exception as e:
        return False, f"[FAILED]  {userhost}: {e}"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="唤醒 worker（只跑 start.sh，不动任务状态）")
    ap.add_argument("targets", nargs="*",
                    help="root@ip / 裸 ip；留空则唤醒 servers.txt 里全部 worker")
    args = ap.parse_args()

    servers_path = Path("servers.txt")
    if not servers_path.exists():
        print(f"[ERROR] 找不到 {servers_path.resolve()}（请在项目根目录运行）")
        return 2

    scheduler_url = scheduler_url_from(servers_path)

    if args.targets:
        targets = [(t if "@" in t else f"root@{t}") for t in args.targets]
    else:
        targets = worker_hosts_from(servers_path)
    if not targets:
        print("[ERROR] 没有待唤醒的 worker")
        return 2

    print(f"==> [start] scheduler={scheduler_url}  待唤醒 {len(targets)} 台")
    for uh in targets:
        print(f"    {uh}")
    print()

    opts = deploy.ssh_opts()
    ok_n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(start_one, uh, scheduler_url, opts): uh for uh in targets}
        for fut in concurrent.futures.as_completed(futs):
            ok, msg = fut.result()
            deploy.log(msg)
            ok_n += 1 if ok else 0

    total = len(targets)
    print(f"\n==> [start] 完成：成功 {ok_n}/{total}")
    print("    验证: curl http://" + scheduler_url.split('//')[1] + ":28765/stats")
    return 0 if ok_n == total else 1


if __name__ == "__main__":
    sys.exit(main())
