#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行部署脚本（替代串行的 deploy.sh 主循环）。

用法：
    python scripts/deploy.py servers.txt
    python scripts/deploy.py servers.txt --parallel 12

行为与原 deploy.sh 一致：
  1. 产物缺失时自动从 dist/ 或项目根目录的 pdf-distribute-v<ver>.zip 解压
  2. 解析 servers.txt（root@ip app=scheduler|worker），自动识别 scheduler IP
  3. 并行执行：scp 上传 tarball -> 远程停旧进程 -> 解压 -> 启动 -> 存活检查
  4. 汇总成功/失败，任一失败退出码非 0

环境变量：
  VERSION           覆盖版本号（默认读 VERSION 文件）
  SSH_OPTS          覆盖 ssh 参数（默认内置密钥+严格选项）
  DEPLOY_PARALLEL   并发数（默认 8）
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
DIST = PROJ / "dist"
DIR_WK = "/opt/pdf-worker"
DIR_SC = "/opt/pdf-scheduler"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def read_version() -> str:
    v = os.getenv("VERSION")
    if v:
        return v.strip()
    f = PROJ / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "0.1.0"


def ensure_artifacts(version: str) -> tuple[Path, Path]:
    """确保 dist/ 下有当前版本的 worker/scheduler tarball，没有就从 zip 解压。"""
    pkg_wk = DIST / f"pdf-distribute-worker-{version}.tar.gz"
    pkg_sc = DIST / f"pdf-distribute-scheduler-{version}.tar.gz"
    if pkg_wk.exists() and pkg_sc.exists():
        return pkg_wk, pkg_sc

    print(f"==> [deploy] v{version} 产物不在 dist/ 下，搜索 zip...")
    for candidate in (DIST / f"pdf-distribute-v{version}.zip", PROJ / f"pdf-distribute-v{version}.zip"):
        if candidate.exists():
            print(f"==> [deploy] 找到 {candidate.name}，解压到 dist/...")
            DIST.mkdir(exist_ok=True)
            with zipfile.ZipFile(candidate) as zf:
                zf.extractall(DIST)
            break
    else:
        print("[ERROR] 找不到 pdf-distribute-v{}.zip".format(version))
        print("    已搜索路径：")
        print(f"      - {DIST / f'pdf-distribute-v{version}.zip'}")
        print(f"      - {PROJ / f'pdf-distribute-v{version}.zip'}")
        sys.exit(2)

    if not (pkg_wk.exists() and pkg_sc.exists()):
        print("[ERROR] 解压后仍缺少 tarball")
        sys.exit(2)
    return pkg_wk, pkg_sc


def parse_servers(path: Path) -> list[tuple[str, str]]:
    targets = []
    seen: set[tuple[str, str]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        userhost = parts[0]
        app = "worker"
        for kv in parts[1:]:
            if kv.startswith("app="):
                app = kv[4:]
        # 同一 host 可有多行不同角色（如同时跑 scheduler+worker），但完全重复的行去掉
        if (userhost, app) not in seen:
            seen.add((userhost, app))
            targets.append((userhost, app))
    return targets


def ssh_opts() -> list[str]:
    env = os.getenv("SSH_OPTS")
    if env:
        return env.split()
    key = Path.home() / ".ssh" / "id_ed25519"
    opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if key.exists():
        opts = ["-i", str(key)] + opts
    return opts


def build_remote_script(app: str, pkg_name: str, dir_: str, scheduler_url: str) -> str:
    """生成远程执行脚本。Python 侧已把所有值代入，注意保留远程才展开的 $(...)。"""
    pkill_target = "worker.main" if app == "worker" else "uvicorn scheduler.main"
    env_line = f"export SCHEDULER_URL={scheduler_url}\n" if app == "worker" else ""
    return f"""set -uo pipefail
mkdir -p {dir_}/data/logs
# 停掉旧进程（如果在跑）
if [[ -f {dir_}/data/{app}.pid ]] && kill -0 "$(cat {dir_}/data/{app}.pid)" 2>/dev/null; then
  kill "$(cat {dir_}/data/{app}.pid)" 2>/dev/null || true
  sleep 1
fi
# 兜底：清理绕过 PID 文件启动的残留进程（注意会打断执行中的 chunk，请尽量无任务时部署）
pkill -f "{pkill_target}" 2>/dev/null || true
sleep 1
# 日志按版本归档：旧日志移成 .旧版本.时间戳.log，新版本从空日志开始
# （包内 VERSION 此刻还是旧版本，tar 覆盖后才是新版本）
LOGF={dir_}/data/logs/{app}.log
if [[ -f "$LOGF" ]]; then
  OLD_VER=unknown
  [[ -f {dir_}/VERSION ]] && OLD_VER="$(cat {dir_}/VERSION)"
  mv "$LOGF" "{dir_}/data/logs/{app}.v${{OLD_VER}}.$(date +%Y%m%d_%H%M%S).log"
fi
tar -xzf /tmp/{pkg_name} -C {dir_} || {{ echo "[FAILED] tar 解压失败"; exit 1; }}
cd {dir_} || exit 1
# 清理历史版本/手工部署的残留目录（当前包结构已不含这些）
rm -rf venv bin lib site-packages
# 清理旧的 jobs/tasks，避免 scheduler recover() 重新载入历史状态
rm -f {dir_}/data/jobs/*.json {dir_}/data/tasks/*.json
{env_line}nohup ./start.sh > {dir_}/data/logs/{app}.log 2>&1 &
echo $! > {dir_}/data/{app}.pid
sleep 3
# 存活检查：启动后 3 秒进程还在才算成功
if kill -0 "$(cat {dir_}/data/{app}.pid)" 2>/dev/null; then
  echo "[started] pid=$(cat {dir_}/data/{app}.pid) {app}"
else
  echo "[FAILED] {app} 启动后 3 秒内退出，最近日志："
  tail -n 20 {dir_}/data/logs/{app}.log || true
  exit 1
fi
"""


def run(cmd: list[str], timeout: int, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)


def deploy_one(userhost: str, app: str, pkg: Path, scheduler_url: str, opts: list[str], attempts: int = 2) -> tuple[bool, str]:
    dir_ = DIR_SC if app == "scheduler" else DIR_WK
    tmp = f"/tmp/{pkg.name}"
    t0 = time.time()
    last_err = ""
    for i in range(1, attempts + 1):
        try:
            # 1. 上传
            r = run(["scp", *opts, str(pkg), f"{userhost}:{tmp}"], timeout=3600)
            if r.returncode != 0:
                last_err = "scp 失败: " + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-500:]
                continue
            # 2. 远程执行（脚本经 stdin 传入，避免引号转义问题）
            script = build_remote_script(app, pkg.name, dir_, scheduler_url)
            r = run(["ssh", *opts, userhost, "bash -s"], timeout=300, input_bytes=script.encode("utf-8"))
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            if r.returncode != 0:
                last_err = "远程执行失败: " + (out + "\n" + err).strip()[-800:]
                continue
            cost = time.time() - t0
            return True, f"[OK]      {userhost} ({app}, {cost:.0f}s)\n{out}"
        except subprocess.TimeoutExpired:
            last_err = f"超时（第 {i}/{attempts} 次尝试）"
    return False, f"[FAILED]  {userhost} ({app}): {last_err}"


def main() -> int:
    ap = argparse.ArgumentParser(description="并行部署 pdf-dispatch")
    ap.add_argument("servers", help="servers.txt 路径")
    ap.add_argument("--parallel", type=int, default=int(os.getenv("DEPLOY_PARALLEL", "8")),
                    help="并发数（默认 8，可用环境变量 DEPLOY_PARALLEL 覆盖）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验产物和服务器列表，不实际部署")
    args = ap.parse_args()

    servers_path = Path(args.servers)
    if not servers_path.exists():
        print(f"[ERROR] 找不到 {servers_path}")
        return 2

    version = read_version()
    pkg_wk, pkg_sc = ensure_artifacts(version)

    targets = parse_servers(servers_path)
    if not targets:
        print("[ERROR] servers.txt 里没有有效行")
        return 2

    scheduler_ip = next((uh.split("@")[-1] for uh, app in targets if app == "scheduler"), "")
    if not scheduler_ip:
        print("[ERROR] servers.txt 里没找到 app=scheduler 的行")
        return 2
    scheduler_url = f"http://{scheduler_ip}:28765"

    print(f"==> [deploy] v{version}  scheduler={scheduler_url}  targets={len(targets)}  并发={args.parallel}")
    for uh, app in targets:
        print(f"    {uh}  {app}")
    print()

    opts = ssh_opts()
    if args.dry_run:
        print("==> [dry-run] 校验通过：产物就绪，未执行任何远程操作")
        for pkg in (pkg_wk, pkg_sc):
            print(f"    {pkg.name}  {pkg.stat().st_size / 1e6:.0f} MB")
        return 0

    results: list[tuple[bool, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = {
            pool.submit(deploy_one, uh, app, pkg_sc if app == "scheduler" else pkg_wk, scheduler_url, opts): (uh, app)
            for uh, app in targets
        }
        for fut in concurrent.futures.as_completed(futs):
            ok, msg = fut.result()
            log(msg)
            results.append((ok, msg))

    ok_n = sum(1 for ok, _ in results if ok)
    print(f"\n==> [deploy] 完成：成功 {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
