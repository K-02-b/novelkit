"""作品目录级操作：重命名、删除（移入回收站）。

为什么单独一个模块
------------------
作品名就是目录名，改名/删除都属于**破坏性**操作，规则要与「导入 EPUB」完全一致
（`import_epub` 也复用这里的 `valid_work_name`），所以集中在同一处，避免两套校验。

安全约定
--------
* 删除不是真删：整个目录**移动**到工作区根的 `.trash/`（带时间戳），随时能捞回来；
* 重命名先复制到临时目录、成功后再删除原目录，避免"复制到一半失败"；
* 目标名已存在时直接拒绝，不做任何写入。
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TRASH_DIRNAME = ".trash"

_BAD_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class WorkError(Exception):
    """参数/状态不合法，调用方据此返回 400。"""


def valid_work_name(name: str) -> bool:
    """作品名就是目录名：允许中文等任何语言的字符，只挡路径分隔符与控制字符。"""
    name = (name or "").strip()
    if not name or name.startswith("."):
        return False
    if len(name) > 64:
        return False
    return not _BAD_NAME.search(name)


def _existing_work_dirs(root: Path) -> set:
    """工作区里现有的作品目录名（用于拒绝重名）。"""
    names = set()
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            if any(re.match(r"^\d+_origin\.txt$", n) for n in os.listdir(entry)):
                names.add(entry.name)
        except OSError:
            continue
    return names


def rename(work_dir: Path, new_name: str, *, root: Path) -> Dict[str, Any]:
    """把作品目录改名为 new_name（同目录下）。"""
    new_name = (new_name or "").strip()
    if not valid_work_name(new_name):
        raise WorkError("作品名不能为空，也不能包含 / \\ : * ? \" < > | 等字符")
    if new_name == work_dir.name:
        return {"ok": True, "unchanged": True, "work": work_dir.name, "old": work_dir.name}

    target = (root / new_name).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:                              # pragma: no cover
        raise WorkError("非法的作品名") from exc
    if target.exists():
        raise WorkError(f"「{new_name}」已经存在，换一个名字")

    # 先整树复制到临时目录，成功后再删原目录：任何一步失败都不会丢数据
    temp = root / f".rename-{os.getpid()}-{datetime.datetime.now().strftime('%H%M%S%f')}"
    try:
        shutil.copytree(work_dir, temp)
    except OSError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise WorkError(f"复制失败：{exc}") from exc
    try:
        os.replace(temp, target)
    except OSError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise WorkError(f"改名失败：{exc}") from exc
    try:
        shutil.rmtree(work_dir)
    except OSError as exc:
        raise WorkError(f"新目录已就绪，但删除旧目录失败：{exc}") from exc

    return {"ok": True, "work": new_name, "old": work_dir.name, "unchanged": False}


def delete(work_dir: Path, *, root: Path) -> Dict[str, Any]:
    """把整部作品移入工作区根的 .trash/（不直接删，随时可恢复）。"""
    trash = root / TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = trash / f"{work_dir.name}-{stamp}"
    counter = 1
    while target.exists():
        counter += 1
        target = trash / f"{work_dir.name}-{stamp}-{counter}"
    try:
        shutil.move(str(work_dir), str(target))
    except OSError as exc:
        raise WorkError(f"移入回收站失败：{exc}") from exc
    try:
        rel = str(target.relative_to(root))
    except ValueError:                                     # pragma: no cover
        rel = str(target)
    return {"ok": True, "work": work_dir.name, "trash_path": rel,
            "files": sum(1 for _ in target.rglob("*") if _.is_file())}
