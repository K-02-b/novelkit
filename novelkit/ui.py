"""终端配色与日志输出。

统一 scripts/ 下各命令行入口的提示风格，并把调试输出收敛到一个对象里，
避免旧版 `debug_print` 每写一行就开关一次文件的低效做法。
"""

from __future__ import annotations

import os
import sys
from typing import Optional


class Color:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


_COLOR_ON = _supports_color()


def c(text: str, color: str) -> str:
    """按需上色（非 TTY 或 NO_COLOR 时原样返回，便于重定向到文件）。"""
    if not _COLOR_ON:
        return text
    return f"{color}{text}{Color.END}"


def info(msg: str) -> None:
    print(f"{c('[提示]', Color.CYAN)} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{c('[完成]', Color.GREEN)} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{c('[警告]', Color.YELLOW)} {msg}", flush=True)


def error(msg: str) -> None:
    print(f"{c('[错误]', Color.RED)} {msg}", file=sys.stderr, flush=True)


class DebugLog:
    """调试输出目标：console（直接打印）/ log（写文件）/ 关闭。

    以追加方式打开一次日志文件并复用句柄，比旧实现每行开关一次文件快得多；
    同时提供 close()/上下文管理，保证句柄释放。
    """

    def __init__(self, mode: Optional[str] = None, log_file: Optional[str] = None):
        self.mode = mode
        self.path = log_file
        self._fh = None
        if self.mode == "log" and log_file:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)) or ".", exist_ok=True)
            self._fh = open(log_file, "a", encoding="utf-8")

    @property
    def enabled(self) -> bool:
        return bool(self.mode)

    def write(self, content: str) -> None:
        if self.mode == "console":
            print(content, flush=True)
        elif self.mode == "log" and self._fh:
            self._fh.write(content + "\n")
            self._fh.flush()

    def banner(self, title: str) -> None:
        self.write(f"\n{'=' * 10} {title} {'=' * 10}")

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "DebugLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def build_log_path(log_dir: str, prefix: str = "") -> str:
    """生成形如 log/20260416_120000.log 的日志路径。"""
    import datetime

    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{prefix}{stamp}.log" if prefix else f"{stamp}.log"
    return os.path.join(log_dir, name)
