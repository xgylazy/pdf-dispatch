#!/bin/bash
# -*- coding: utf-8 -*-
# 把本机 SSH 公钥批量抄到 servers.txt 里的所有目标机。
#
# 前提（msys2 / Linux 均适用）：
#   本机已 ssh-keygen 生成默认密钥 ~/.ssh/id_ed25519[.pub]
#
# 用法：
#   ./scripts/setup_keys.sh                        # 每台机器会各自提示输入密码
#   ./scripts/setup_keys.sh -p MyPassword          # 所有机器同一密码
#   ./scripts/setup_keys.sh -f passwords.txt       # 每行：user@host:password
#
# 完成后跑 ./scripts/deploy.sh servers.txt 即可无密码部署。
set -euo pipefail

SERVERS="servers.txt"
KEY="${HOME}/.ssh/id_ed25519"
PUB="${KEY}.pub"
PASS_FILE=""
PASS_VAL=""

while getopts "p:f:" opt; do
  case $opt in
    p) PASS_VAL="$OPTARG" ;;
    f) PASS_FILE="$OPTARG" ;;
  esac
done
[[ $# -gt 0 && "${1:-}" != -* ]] && SERVERS="$1"

# 自动生成密钥（不存在时）
if [[ ! -f "$PUB" ]]; then
  ssh-keygen -t ed25519 -f "$KEY" -N "" -q
  echo "[ok] 已生成密钥 ${KEY}"
fi

# msys2 自带 ssh-copy-id；Linux 一般也有；sshpass 需要时再用
have_sshpass() { command -v sshpass >/dev/null 2>&1; }

parse_target() {
  local line="$1" host_part user host
  host_part="${line%% *}"       # 只取第一个字段 user@app=host
  host="${host_part#*@}"
  user="${host_part%@*}"
  printf '%s\n%s' "$user" "$host"
}

COPIED=0
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// /}" ]] && continue
  arr=($(parse_target "$line"))
  u="${arr[0]}"; h="${arr[1]}"
  target="${u}@${h}"
  pw=""
  if [[ -n "$PASS_FILE" ]]; then
    pw=$(grep "^${h}" "$PASS_FILE" 2>/dev/null | head -1 | cut -d: -f2- || true)
  fi
  [[ -z "$pw" ]] && pw="$PASS_VAL"

  echo "==> ${target}"
  if [[ -n "$pw" ]] && have_sshpass; then
    sshpass -p "$pw" ssh-copy-id -i "$PUB" -o StrictHostKeyChecking=no "$target"
  else
    ssh-copy-id -i "$PUB" -o StrictHostKeyChecking=no "$target"
  fi
  COPIED=$((COPIED + 1))
done < "$SERVERS"

echo
echo "[done] 已配置 ${COPIED} 台机器。现在可以跑： ./scripts/deploy.sh ${SERVERS}"
