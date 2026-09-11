#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中转部署：笔记本只把 zip 传给 scheduler 一台，内网分发由 scheduler 完成。

用法（项目根目录，和 deploy.py 一样）：
    python scripts/deploy_relay.py servers.txt

适用场景：笔记本到内网之间是高延迟/抖动的慢链路（VPN 等），
但内网机器之间是快网。流程：
  1. 本地把 pdf-distribute-v{VERSION}.zip 传到 scheduler:/tmp/（唯一一次慢链路传输）
  2. scheduler 用包内自带 Python 解压 zip
  3. scheduler 把本机 SSH 私钥副本 + 各 tarball 经内网分发到所有目标机
  4. 各目标机执行与 deploy.py 完全相同的部署脚本（停旧进程→解压→启动→存活检查）
  5. 汇总成功/失败

安全提示：本机 SSH 私钥会以 600 权限临时放在 scheduler 的 /tmp/ 下
（重启自动清除）。介意的话用完手动 rm /tmp/pdf_relay_key。
"""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402  复用产物定位/ssh选项/远程脚本

DIR_WK, DIR_SC = "/opt/pdf-worker", "/opt/pdf-scheduler"


def find_zip(version: str) -> Path:
    for cand in (deploy.DIST / f"pdf-distribute-v{version}.zip",
                 deploy.PROJ / f"pdf-distribute-v{version}.zip"):
        if cand.exists():
            return cand
    print(f"[ERROR] 找不到 pdf-distribute-v{version}.zip，已搜索：")
    print(f"    {deploy.DIST / f'pdf-distribute-v{version}.zip'}")
    print(f"    {deploy.PROJ / f'pdf-distribute-v{version}.zip'}")
    sys.exit(2)


def build_orchestrator(version: str, zip_name: str, key_b64: str,
                       targets: list[tuple[str, str]], scheduler_url: str,
                       scheduler_host: str) -> str:
    """生成在 scheduler 上执行的编排脚本（内网分发 + 并行部署）。"""
    pkg_wk = f"pdf-distribute-worker-{version}.tar.gz"
    pkg_sc = f"pdf-distribute-scheduler-{version}.tar.gz"
    ssh_base = 'ssh -i /tmp/pdf_relay_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes'
    scp_base = 'scp -i /tmp/pdf_relay_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes'

    lines = [
        "set -uo pipefail",
        "RELAY=/tmp/pdf-relay",
        'mkdir -p "$RELAY" && rm -rf "$RELAY"/*',
        f'echo "[relay] 解压 {zip_name} ..."',
        f'/opt/pdf-scheduler/python/bin/python3.11 -m zipfile -e "/tmp/{zip_name}" "$RELAY" '
        '|| { echo "[relay][FATAL] zip 解压失败"; exit 1; }',
        f'[ -f "$RELAY/{pkg_wk}" ] && [ -f "$RELAY/{pkg_sc}" ] '
        '|| { echo "[relay][FATAL] zip 内缺少 tarball"; exit 1; }',
        f'echo "{key_b64}" | base64 -d > /tmp/pdf_relay_key && chmod 600 /tmp/pdf_relay_key',
    ]

    bg_jobs = []
    for i, (userhost, app) in enumerate(targets):
        pkg = pkg_sc if app == "scheduler" else pkg_wk
        dir_ = DIR_SC if app == "scheduler" else DIR_WK
        script_b64 = base64.b64encode(
            deploy.build_remote_script(app, pkg, dir_, scheduler_url).encode("utf-8")).decode()
        log = f"/tmp/pdf_relay_{i}.log"
        lines.append(f"# ---- target {i}: {userhost} ({app}) ----")
        if userhost == scheduler_host and app == "scheduler":
            # scheduler 本机：tarball 就地拷贝，部署脚本本地执行
            lines.append(f'( cp "$RELAY/{pkg}" "/tmp/{pkg}" && \\')
            lines.append(f'  echo "{script_b64}" | base64 -d | bash -s ) > {log} 2>&1 &')
        else:
            lines.append(f'( {scp_base} "$RELAY/{pkg}" "{userhost}:/tmp/{pkg}" && \\')
            lines.append(f'  echo "{script_b64}" | base64 -d > /tmp/pdf_relay_deploy.sh && \\')
            lines.append(f'  {scp_base} /tmp/pdf_relay_deploy.sh "{userhost}:/tmp/pdf_relay_deploy.sh" && \\')
            lines.append(f'  {ssh_base} "{userhost}" "bash /tmp/pdf_relay_deploy.sh" ) > {log} 2>&1 &')
        bg_jobs.append((i, userhost, app, log))

    lines.append("wait")
    lines.append('echo "===== RELAY RESULTS ====="')
    lines.append("ok=0")
    for i, userhost, app, log in bg_jobs:
        lines.append(f'echo "--- {userhost} ({app}) ---"')
        lines.append(f"cat {log} 2>/dev/null")
        lines.append(f'if grep -q "\\[started\\]" {log} 2>/dev/null; then '
                     f'ok=$((ok+1)); echo "RELAY_RESULT {userhost} {app} OK"; '
                     f'else echo "RELAY_RESULT {userhost} {app} FAILED"; fi')
    lines.append(f'echo "RELAY_SUMMARY ok=$ok total={len(targets)}"')
    return "\n".join(lines)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="中转部署：zip 只传 scheduler，内网完成分发")
    ap.add_argument("servers", nargs="?", default="servers.txt", help="servers.txt 路径")
    ap.add_argument("--zip", help="手动指定 zip 路径（默认自动搜索）")
    args = ap.parse_args()

    servers_path = Path(args.servers)
    if not servers_path.exists():
        print(f"[ERROR] 找不到 {servers_path.resolve()}（请在项目根目录运行）")
        return 2

    version = deploy.read_version()
    zip_path = Path(args.zip) if args.zip else find_zip(version)
    print(f"==> [relay] v{version}  zip={zip_path.name}  ({zip_path.stat().st_size / 1e6:.0f} MB)")

    all_targets = deploy.parse_servers(servers_path)
    if not all_targets:
        print("[ERROR] servers.txt 为空")
        return 2
    scheduler_host = next((uh for uh, a in all_targets if a == "scheduler"), None)
    if not scheduler_host:
        print("[ERROR] servers.txt 里没有 app=scheduler 的行")
        return 2
    scheduler_url = f"http://{scheduler_host.split('@')[-1]}:28765"

    key_path = Path.home() / ".ssh" / "id_ed25519"
    if not key_path.exists():
        print(f"[ERROR] 找不到本机私钥 {key_path}")
        return 2
    key_b64 = base64.b64encode(key_path.read_bytes()).decode()

    # 去重后按 servers.txt 顺序分发
    targets = list(dict.fromkeys(all_targets))

    # 1) zip 传到 scheduler（唯一一次慢链路大传输，内建重试）
    zip_name = zip_path.name
    opts = deploy.ssh_opts()
    print(f"==> [relay] 1/2 上传 zip 到 {scheduler_host}:/tmp/{zip_name} ...")
    sent = False
    for attempt in (1, 2, 3):
        r = subprocess.run(["scp", *opts, str(zip_path), f"{scheduler_host}:/tmp/{zip_name}"],
                           capture_output=True, timeout=7200)
        if r.returncode == 0:
            sent = True
            break
        print(f"    [warn] 第 {attempt} 次上传失败："
              f"{(r.stderr or b'').decode('utf-8', 'replace').strip()[-200:]}")
    if not sent:
        print("[ERROR] zip 上传失败（3 次）")
        return 1
    print("    [ok] zip 已上传")

    # 2) 在 scheduler 上执行编排脚本（内网分发 + 并行部署）
    orchestrator = build_orchestrator(version, zip_name, key_b64,
                                      targets, scheduler_url, scheduler_host)
    print(f"==> [relay] 2/2 在 scheduler 上分发并部署 {len(targets)} 台（内网并行）...")
    proc = subprocess.Popen(["ssh", *opts, scheduler_host, "bash -s"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    proc.stdin.write(orchestrator.encode("utf-8"))
    proc.stdin.close()
    assert proc.stdout is not None
    summary_ok, summary_total = 0, len(targets)
    results: dict[str, str] = {}
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if line.startswith("RELAY_RESULT"):
            parts = line.split()
            results[parts[1]] = parts[3]
            continue
        if line.startswith("RELAY_SUMMARY"):
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                if k == "ok":
                    summary_ok = int(v)
            continue
        print(line, flush=True)
    proc.wait()

    print(f"\n==> [relay] 完成：成功 {summary_ok}/{summary_total}")
    if summary_ok < summary_total:
        failed = [f"{uh}" for uh in results if results[uh] == "FAILED"]
        print("    失败机器：" + " ".join(failed))
        if all(a == "worker" for _, a in [(u, a) for u, a in targets if u in failed]):
            print("    重试: python scripts/redeploy.py " + " ".join(failed))
        else:
            print("    重试: python scripts/deploy_relay.py " + " ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
