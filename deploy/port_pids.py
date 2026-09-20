#!/usr/bin/env python3
"""找出正在监听指定 TCP 端口的进程 PID。

为什么不用 `ss -ltnp` / `lsof -i` / `fuser`：
在本机（受限环境）里它们的 Process 列是空的、或直接查不到——而我们要靠这个
"释放端口"再启动服务，静默失败就会让服务因 bind 失败反复重启。
这里直接读 `/proc/net/tcp` 拿监听套接字的 inode，再扫 `/proc/*/fd` 反查 PID，
同用户进程一定能查到，不依赖外部命令与额外权限。

用法：
    python3 deploy/port_pids.py 8897 18423      # 每行一个 PID
    python3 deploy/port_pids.py --check 8897    # 有占用则退出码 1
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Set

LISTEN = "0A"          # /proc/net/tcp 里的状态码：0A = LISTEN


def listening_inodes(ports: Set[int]) -> Dict[int, Set[str]]:
    """返回 {端口: {inode, ...}}。"""
    wanted = {f":{port:04X}": port for port in ports}
    found: Dict[int, Set[str]] = {port: set() for port in ports}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                next(handle, None)      # 跳过表头
                for line in handle:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != LISTEN:
                        continue
                    local = fields[1]
                    for suffix, port in wanted.items():
                        if local.endswith(suffix):
                            found[port].add(fields[9])
        except OSError:
            continue
    return found


def pids_by_inode() -> Dict[str, int]:
    """扫描 /proc/*/fd，建立 socket inode → PID 的映射。"""
    mapping: Dict[str, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{entry}/fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue                # 进程已退出 / 无权限
        for name in names:
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                mapping.setdefault(target[8:-1], pid)
    return mapping


def pids_for(ports) -> Dict[int, Set[int]]:
    inodes = listening_inodes({int(p) for p in ports})
    by_inode = pids_by_inode()
    result: Dict[int, Set[int]] = {}
    for port, found in inodes.items():
        result[port] = {by_inode[ino] for ino in found if ino in by_inode}
    return result


def main(argv) -> int:
    check = False
    args = list(argv)
    if args and args[0] == "--check":
        check = True
        args = args[1:]
    if not args:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    found = pids_for(args)
    occupied = False
    for port, pids in found.items():
        if pids:
            occupied = True
            print(" ".join(str(pid) for pid in sorted(pids)))
    if check:
        return 1 if occupied else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
