#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机 SSH 公钥批量装到 servers.txt 里所有目标机（并行）。

用法（项目根目录）：
    python scripts/setup_keys.py -p 123.com          # 所有机器同一密码 -> 并行
    python scripts/setup_keys.py -f passwords.txt    # 不同密码 -> 并行（host:password 每行）
    python scripts/setup_keys.py                     # 无密码参数 -> 交互式逐台（串行回退）
    python scripts/setup_keys.py -p 123.com --parallel 13

说明：
  * 已经免密的机器（如之前配过的）自动跳过，不再要密码
  * 密码模式依赖 paramiko（pip install paramiko），Windows 无需 sshpass
  * 配完即可 python scripts/deploy.py servers.txt 免密并行部署
"""
from __future__ import annotations

import argparse
import concurrent.futures
import shlex
import subprocess
import sys
import threading
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def ensure_key(key_path: Path) -> Path:
    """确保本机有 ed25519 密钥，返回 .pub 路径。"""
    pub = key_path.with_suffix(key_path.suffix + ".pub") if key_path.suffix != ".pub" else key_path
    if not pub.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"], check=True)
        print(f"[ok] 已生成密钥 {key_path}")
    return pub


def parse_servers(path: Path) -> list[tuple[str, str]]:
    """返回 [(user, host)] 列表，按 user@host 去重（同一台机器配一次公钥即可，
    servers.txt 里允许同一 host 出现多行不同 app= 角色）。"""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        userhost = line.split()[0]
        user, _, host = userhost.partition("@")
        if not host:
            user, host = "root", user
        if (user, host) not in seen:
            seen.add((user, host))
            out.append((user, host))
    return out


def parse_password_file(path: Path) -> dict[str, str]:
    """每行 host:password 或 user@host:password。"""
    pw = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        host_part, _, password = line.rpartition(":")
        host = host_part.rpartition("@")[2] or host_part
        pw[host] = password
    return pw


def already_key_auth(user: str, host: str) -> bool:
    """BatchMode 试连：能免密登录说明公钥已装好。"""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=8", f"{user}@{host}", "true"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def install_key_paramiko(user: str, host: str, password: str, pub_line: str) -> tuple[bool, str]:
    """用 paramiko 登录并追加公钥（幂等：已存在不重复写）。"""
    try:
        import paramiko
    except ImportError:
        return False, "缺少 paramiko，请先执行: pip install paramiko"

    cmd = (
        "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
        f"grep -qxF {shlex.quote(pub_line)} ~/.ssh/authorized_keys || "
        f"echo {shlex.quote(pub_line)} >> ~/.ssh/authorized_keys; "
        "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
    )
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=user, password=password, timeout=10,
                    look_for_keys=False, allow_agent=False)
        _, stdout, stderr = cli.exec_command(cmd, timeout=15)
        rc = stdout.channel.recv_exit_status()
        if rc == 0:
            return True, ""
        return False, (stderr.read() or b"").decode("utf-8", "replace").strip()[-300:] or f"exit {rc}"
    except Exception as e:
        return False, str(e)[:300]
    finally:
        cli.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="并行批量安装 SSH 公钥")
    ap.add_argument("servers", nargs="?", default="servers.txt", help="servers.txt 路径")
    ap.add_argument("-p", "--password", help="所有机器同一密码（启用并行）")
    ap.add_argument("-f", "--passwords", help="密码文件，每行 host:password 或 user@host:password")
    ap.add_argument("--parallel", type=int, default=8, help="并发数（默认 8）")
    ap.add_argument("--key", default=str(Path.home() / ".ssh" / "id_ed25519"), help="私钥路径")
    args = ap.parse_args()

    servers_path = Path(args.servers)
    if not servers_path.exists():
        print(f"[ERROR] 找不到 {servers_path}")
        return 2

    pub = ensure_key(Path(args.key))
    pub_line = pub.read_text(encoding="utf-8").strip()
    targets = parse_servers(servers_path)
    print(f"==> 共 {len(targets)} 台目标机，密钥 {pub.name}")

    pw_map = parse_password_file(Path(args.passwords)) if args.passwords else {}

    # 先分拣：已免密的跳过；有密码的并行装；没密码的进交互串行队列
    done, interactive, failed = [], [], []
    pending_pw: list[tuple[str, str, str]] = []
    for user, host in targets:
        log(f"==> 检查 {user}@{host} ...")
        if already_key_auth(user, host):
            log(f"    [skip] 已免密")
            done.append(f"{user}@{host}")
        else:
            password = pw_map.get(host, args.password or "")
            if password:
                pending_pw.append((user, host, password))
            else:
                interactive.append((user, host))

    if pending_pw:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            print("[ERROR] 当前 Python 缺少 paramiko，安装方式：")
            print("  Windows 原生 Python / 常规 venv:  pip install paramiko")
            print("  msys2 系统Python:                 pacman -S mingw-w64-x86_64-python-paramiko")
            print("  注意：msys2 下 pacman 装的包对激活的 venv 不可见，请 deactivate 后再跑，")
            print("        或改用 Windows 原生 Python（有预编译 wheel，无需编译 Rust）")
            return 2
        print(f"==> 并行安装公钥：{len(pending_pw)} 台，并发 {args.parallel}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            futs = {pool.submit(install_key_paramiko, u, h, p, pub_line): f"{u}@{h}"
                    for u, h, p in pending_pw}
            for fut in concurrent.futures.as_completed(futs):
                target = futs[fut]
                ok, err = fut.result()
                if ok:
                    log(f"    [ok]   {target}")
                    done.append(target)
                else:
                    log(f"    [FAIL] {target}: {err}")
                    failed.append(target)

    # 交互模式回退：逐台 ssh-copy-id（终端里会提示输密码）
    for user, host in interactive:
        target = f"{user}@{host}"
        print(f"==> {target}（请输入密码）")
        r = subprocess.run(["ssh-copy-id", "-i", str(pub),
                            "-o", "StrictHostKeyChecking=no", target])
        if r.returncode == 0:
            done.append(target)
        else:
            failed.append(target)

    print(f"\n[done] 成功 {len(done)}/{len(targets)}，失败 {len(failed)}")
    if failed:
        print("  失败列表：" + ", ".join(failed))
        print("  提示：确认密码正确、目标机允许密码登录（sshd_config PasswordAuthentication）")
        return 1
    print(f"现在可以跑： python scripts/deploy.py {servers_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
