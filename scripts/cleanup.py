#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 worker / scheduler 机器上的运行时数据（模型+日志+结果+原件等）。

用法（项目根目录）：
    python scripts/cleanup.py                    # 列出 servers.txt 全部机器将清理的内容（预览）
    python scripts/cleanup.py --yes              # 真正执行清理
    python scripts/cleanup.py root@192.192.98.80 --yes   # 只清理指定的机器

清理范围：
  worker    (/opt/pdf-worker)   data/（日志）+ models/（OCR 模型）
  scheduler (/opt/pdf-scheduler) data/（jobs/tasks/results/pdfs 原件/日志）

不动的：部署本身（python/、worker/、start.sh 等），清理后可直接重新提交任务
或重新部署。⚠️ 任务状态也在 data/ 里，清理后所有进行中/历史任务的进度消失。
"""
from __future__ import annotations

import argparse
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
    return ""


def build_cleanup_script(app: str) -> str:
    """返回在目标机上执行的清理脚本：先报体积，再删除，再报剩余磁盘。"""
    base = "/opt/pdf-scheduler" if app == "scheduler" else "/opt/pdf-worker"
    dirs = [f"{base}/data"]
    if app == "worker":
        dirs.append(f"{base}/models")
    du = " ".join(f'"{d}"' for d in dirs)
    rm = " ".join(dirs)
    return f"""echo "[cleanup] {app} 清理范围：{du}"
du -sh {du} 2>/dev/null || echo "(目录不存在，无内容可清理)"
rm -rf {rm}
echo "[cleanup] 已删除。当前磁盘："
df -h / | tail -1
"""


def clean_one(userhost: str, app: str, opts: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["ssh", *opts, userhost, "bash -s"],
                           input=build_cleanup_script(app).encode("utf-8"),
                           capture_output=True, timeout=120)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if r.returncode != 0:
            return False, f"[FAILED]  {userhost} ({app}): {(out + err).strip()[-400:]}"
        return True, f"===== {userhost} ({app}) =====\n{out}\n"
    except subprocess.TimeoutExpired:
        return False, f"[FAILED]  {userhost}: ssh 超时"
    except Exception as e:
        return False, f"[FAILED]  {userhost}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 worker/scheduler 运行时数据")
    ap.add_argument("targets", nargs="*",
                    help="root@ip / 裸 ip；留空则清理 servers.txt 全部机器")
    ap.add_argument("--yes", action="store_true",
                    help="真正执行删除；不带此参数仅预览将清理的内容和体积")
    ap.add_argument("--servers", default="servers.txt", help="servers.txt 路径")
    args = ap.parse_args()

    servers_path = Path(args.servers)
    if not servers_path.exists():
        print(f"[ERROR] 找不到 {servers_path.resolve()}（请在项目根目录运行）")
        return 2

    all_targets = deploy.parse_servers(servers_path)
    if args.targets:
        targets = [(t if "@" in t else f"root@{t}", "worker") for t in args.targets]
    else:
        targets = list(dict.fromkeys(all_targets))
    if not targets:
        print("[ERROR] 没有目标机器")
        return 2

    scheduler_url = scheduler_url_from(servers_path)
    if scheduler_url:
        print(f"==> [cleanup] scheduler={scheduler_url}  目标 {len(targets)} 台  "
              f"模式={'执行删除' if args.yes else '预览（加 --yes 执行）'}")
    opts = deploy.ssh_opts()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(clean_one, uh, app, opts): (uh, app) for uh, app in targets}
        for fut in concurrent.futures.as_completed(futs):
            ok, msg = fut.result()
            deploy.log(msg)
            results.append(ok)

    ok_n = sum(1 for r in results if r)
    print(f"\n==> [cleanup] 完成：成功 {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
