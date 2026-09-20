"""本地章节编辑：直接在作品目录里读写原文/译文，不再依赖任何发布平台。

设计要点
--------
* **写前备份**：每次保存都把旧文件另存到 `.backups/`，文件名带时间戳，
  改错了可以自己还原（面板不提供"撤销"，但文件一直留着）。
* **原子写**：先写同目录临时文件再 `os.replace`，避免写一半被中断留下半截章节。
* **只认工作区内的作品目录**：作品名由调用方校验过，这里再做一次路径检查，
  防止 `../` 之类把写入引到项目外面去。
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_BACKUP_DIRNAME = ".backups"
_SIDES = {
    "origin": ("{num}_origin.txt", "原文"),
    "translated": ("{num}_translated.txt", "译文"),
}


class EditorError(Exception):
    """参数/路径不合法，调用方据此返回 400。"""


class EditorConflict(EditorError):
    """目标已存在（例如"新建章节"撞上已有文件），调用方据此返回 409。"""


def _resolve_target(work_dir: Path, num: int, side: str) -> Path:
    if side not in _SIDES:
        raise EditorError(f"未知的文本类型：{side}")
    if num < 0:
        raise EditorError("章节号不能为负数")
    name = _SIDES[side][0].format(num=num)
    target = (work_dir / name).resolve()
    try:
        target.relative_to(work_dir.resolve())
    except ValueError as exc:      # pragma: no cover - 作品名已在上游校验
        raise EditorError("目标路径越界") from exc
    return target


def read_text(work_dir: Path, num: int, side: str) -> str:
    target = _resolve_target(work_dir, num, side)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise EditorError(f"读取失败：{exc}") from exc


def _backup(target: Path, work_dir: Path) -> str:
    """把当前内容另存到 .backups/，返回相对作品目录的路径（没有旧文件则空串）。"""
    if not target.exists():
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = work_dir / _BACKUP_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / f"{target.name}.bak-{stamp}"
    try:
        backup.write_bytes(target.read_bytes())
    except OSError:
        return ""
    try:
        return str(backup.relative_to(work_dir))
    except ValueError:             # pragma: no cover
        return str(backup)


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_text(work_dir: Path, num: int, side: str, text: str, *,
              create: bool = False) -> Dict[str, Any]:
    """保存一章的原文或译文。text 为空字符串时**不删除**文件，只写空内容。

    create=True 表示"新建章节"：目标文件已经存在时**拒绝覆盖**，
    避免在"新建章节"里误填了已有章号把现成译文清空。
    """
    if not isinstance(text, str):
        raise EditorError("正文必须是字符串")
    target = _resolve_target(work_dir, num, side)
    label = _SIDES[side][1]

    if create and target.exists():
        raise EditorConflict(f"第 {num} 章的{label}已经存在，换个章号，或直接在编辑器里打开它修改")

    previous = ""
    if target.exists():
        try:
            previous = target.read_text(encoding="utf-8")
        except OSError:
            previous = ""

    backup = _backup(target, work_dir)
    try:
        _atomic_write(target, text)
    except OSError as exc:
        raise EditorError(f"写入失败：{exc}") from exc

    return {
        "ok": True,
        "work": work_dir.name,
        "num": num,
        "side": side,
        "label": label,
        "path": target.name,
        "backup": backup,
        "created": not bool(previous) and bool(text),
        "chars": len(text),
        "lines": len([line for line in text.splitlines() if line.strip()]),
        "changed": previous != text,
    }


_ORIGIN_RE = re.compile(r"^(\d+)_origin\.txt$")
_TRANSLATED_RE = re.compile(r"^(\d+)_translated\.txt$")


def inventory(work_dir: Path) -> Dict[str, Any]:
    """列出章号范围与缺原文/缺译文的章节，供编辑器做增章与定位。"""
    origins, translated = set(), set()
    try:
        names = os.listdir(work_dir)
    except OSError:
        names = []
    for name in names:
        match = _ORIGIN_RE.match(name)
        if match:
            origins.add(int(match.group(1)))
            continue
        match = _TRANSLATED_RE.match(name)
        if match:
            translated.add(int(match.group(1)))

    numbers = sorted(origins | translated)
    return {
        "ok": True,
        "work": work_dir.name,
        "numbers": numbers,
        "missing_origin": sorted(translated - origins),
        "missing_translated": sorted(origins - translated),
        "max": numbers[-1] if numbers else 0,
        "next_number": (numbers[-1] + 1) if numbers else 1,
    }


# 删章时要一起带走的每章衍生文件（存在才删）
_CHAPTER_SUFFIXES = (
    "origin.txt", "translated.txt", "back.txt", "review.txt",
    "refine.json",
)


def delete_chapter(work_dir: Path, num: int, *, include_glossary: bool = True) -> Dict[str, Any]:
    """删除一章：正文（原文/译文/回译/评审/精修）默认全删，术语库文件可选。

    会先把要删的文件备份到 .backups/，再删除；返回删掉了哪些。
    """
    if num < 0:
        raise EditorError("章节号不能为负数")

    targets = [work_dir / f"{num}_{suffix}" for suffix in _CHAPTER_SUFFIXES]
    if include_glossary:
        targets.append(work_dir / f"glossary_{num}.json")

    removed, backups = [], []
    for target in targets:
        if not target.exists() or not target.is_file():
            continue
        backup = _backup(target, work_dir)
        try:
            target.unlink()
        except OSError as exc:
            raise EditorError(f"删除 {target.name} 失败：{exc}") from exc
        removed.append(target.name)
        if backup:
            backups.append(backup)

    if not removed:
        raise EditorError(f"第 {num} 章没有可删除的文件")

    # 章节文件变了，术语总表要跟着重建（章节归属来自各章 glossary 文件）
    try:
        sys.path.insert(0, str(_ROOT))
        from novelkit import glossary as nkglossary

        nkglossary.GlossaryStore(str(work_dir)).write_tracker()
    except Exception:  # noqa: BLE001 — tracker 重建失败不该让删除回滚
        pass

    return {"ok": True, "work": work_dir.name, "num": num,
            "removed": removed, "backups": backups, "count": len(removed)}
