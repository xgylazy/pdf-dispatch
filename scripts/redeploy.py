#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补部署指定 worker 机器（部署失败后单独重试用）。

用法（项目根目录）：
    python scripts/redeploy.py root@192.192.98.106
    python scripts/redeploy.py root@192.192.98.106 root@192.192.98.83

行为与全量部署（deploy.py）完全一致，只是只处理点名的机器；已在线的
机器不会被触碰。scheduler 地址自动从 servers.txt 里读取。
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402  复用 deploy.py 的产物定位 / ssh 选项 / 远程脚本


def scheduler_url_from(servers_path: Path) -> str:
    for line in servers_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.endswith("app=scheduler"):
            return f"http://{line.split()[0].split('@')[-1]}:28765"
    print(f"[ERROR] {servers_path} 里没有 app=scheduler 的行")
    sys.exit(2)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/redeploy.py root@192.192.98.106 [root@ip ...]")
        return 2

    version = deploy.read_version()
    pkg_wk, _ = deploy.ensure_artifacts(version)
    scheduler_url = scheduler_url_from(Path("servers.txt"))
    targets = [(t if "@" in t else f"root@{t}", "worker") for t in sys.argv[1:]]

    print(f"==> [redeploy] v{version}  scheduler={scheduler_url}  待补 {len(targets)} 台")
    for uh, _ in targets:
        print(f"    {uh}")

    opts = deploy.ssh_opts()
    ok_n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(deploy.deploy_one, uh, app, pkg_wk, scheduler_url, opts): uh
                for uh, app in targets}
        for fut in concurrent.futures.as_completed(futs):
            ok, msg = fut.result()
            deploy.log(msg)
            ok_n += 1 if ok else 0

    print(f"\n==> [redeploy] 完成：成功 {ok_n}/{len(targets)}")
    return 0 if ok_n == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
