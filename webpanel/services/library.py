"""作品库扫描：作品清单、章节库存、中英对照、术语库与冲突记录。

只依赖标准库，因此在系统 python3 下也能跑（面板的只读视图不需要 requests/bs4）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from novelkit import config as nkconfig  # noqa: E402  (项目根目录已在 sys.path 上)

# 本模块刻意只依赖标准库（面板的只读视图要在没有第三方依赖时也能跑），
# 所以这里自带一个 CJK 判定，不引 novelkit。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 这些目录看起来像作品目录，但其实是项目自身结构
_NON_WORK_DIRS = {
    "webpanel", "novelkit", "tests", "static", "node_modules", "config",
    "scripts", "docs", "deploy", "assets", "works",
    ".venv", "venv", "__pycache__", "bak", "log", "download", "data",
}

_ORIGIN_RE = re.compile(r"^(\d+)_origin\.txt$")
_TRANSLATED_RE = re.compile(r"^(\d+)_translated\.txt$")
_GLOSSARY_RE = re.compile(r"^glossary_(\d+)\.json$")


def project_root() -> Path:
    """webpanel/services/library.py → 项目根目录（代码仓库）。"""
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    """作品工作区：默认 <项目根>/works/，可用 NOVELKIT_WORKSPACE 覆盖。"""
    return nkconfig.workspace_root()


def global_glossary_path() -> Path:
    """全局术语库文件：config/glossary.json。"""
    return nkconfig.GLOBAL_GLOSSARY_PATH


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _word_count(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# 作品
# ---------------------------------------------------------------------------

def is_work_dir(path: Path) -> bool:
    if not path.is_dir() or path.name in _NON_WORK_DIRS or path.name.startswith("."):
        return False
    try:
        for name in path.iterdir():
            if _ORIGIN_RE.match(name.name):
                return True
    except OSError:
        return False
    return False


def list_works(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """列出工作区里的所有作品目录及其库存统计。"""
    root = root or workspace_root()
    works: List[Dict[str, Any]] = []
    if not root.is_dir():
        return works
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not is_work_dir(entry):
            continue
        works.append(work_summary(entry, root))
    return works


def work_summary(work_dir: Path, root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or workspace_root()
    origins: List[int] = []
    translated: List[int] = []
    glossaries: List[int] = []

    for name in os.listdir(work_dir):
        match = _ORIGIN_RE.match(name)
        if match:
            origins.append(int(match.group(1)))
            continue
        match = _TRANSLATED_RE.match(name)
        if match:
            translated.append(int(match.group(1)))
            continue
        match = _GLOSSARY_RE.match(name)
        if match:
            glossaries.append(int(match.group(1)))

    origins.sort()
    translated.sort()
    glossaries.sort()

    translated_set = set(translated)
    pending = [n for n in origins if n not in translated_set]

    conflicts = read_conflicts(work_dir)

    return {
        "name": work_dir.name,
        "path": str(work_dir),
        "chapters": len(origins),
        "translated": len(translated),
        "pending": len(pending),
        "glossaries": len(glossaries),
        "conflicts": len(conflicts),
        "blocked_conflicts": len([c for c in conflicts if c.get("action") == "ignored"]),
        "next_pending": pending[0] if pending else None,
        "has_local_prompt": any((work_dir / n).exists()
                                for n in (nkconfig.WORK_PROMPT_FILENAME, "提示词.txt")),
        "has_tracker": (work_dir / "global_glossary_tracker.json").exists(),
    }


def work_prompt(work_dir: Path) -> str:
    """作品级附加提示词：优先 prompt.txt，兼容旧的 提示词.txt。"""
    for name in (nkconfig.WORK_PROMPT_FILENAME, "提示词.txt"):
        path = work_dir / name
        if path.exists():
            return _read_text(path)
    return ""


def chapter_inventory(work_dir: Path) -> List[Dict[str, Any]]:
    """逐章列出状态、字数、术语数量。"""
    origin_map: Dict[int, Path] = {}
    translated_map: Dict[int, Path] = {}
    glossary_map: Dict[int, Path] = {}

    for name in os.listdir(work_dir):
        match = _ORIGIN_RE.match(name)
        if match:
            origin_map[int(match.group(1))] = work_dir / name
            continue
        match = _TRANSLATED_RE.match(name)
        if match:
            translated_map[int(match.group(1))] = work_dir / name
            continue
        match = _GLOSSARY_RE.match(name)
        if match:
            glossary_map[int(match.group(1))] = work_dir / name

    rows: List[Dict[str, Any]] = []
    for number in sorted(set(origin_map) | set(translated_map)):
        zh_path = origin_map.get(number)
        en_path = translated_map.get(number)
        zh = _read_text(zh_path) if zh_path else ""
        en = _read_text(en_path) if en_path else ""

        glossary = _read_json(glossary_map[number], {}) if number in glossary_map else {}
        terms = sum(len(v) for v in glossary.values()) if isinstance(glossary, dict) else 0

        rows.append({
            "num": number,
            "has_origin": zh_path is not None,
            "has_translated": en_path is not None,
            "zh_chars": _cjk_count(zh),
            "en_words": _word_count(en),
            "terms": terms,
            "is_intro": number == 0,
        })
    return rows


# ---------------------------------------------------------------------------
# 中英对照
# ---------------------------------------------------------------------------

def split_paragraphs(text: str) -> List[str]:
    """按行切段并去掉空行（原文与译文都是"一行一段"的格式）。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


from novelkit import align as _align  # noqa: E402  (项目根目录已在 sys.path 上)

split_paragraphs = _align.split_paragraphs
align_paragraphs = _align.align_paragraphs
_align_cached = _align.align_cached


def read_chapter(work_dir: Path, number: int) -> Dict[str, Any]:
    """读取一章的中英对照数据 + 本章术语库。"""
    zh_path = work_dir / f"{number}_origin.txt"
    en_path = work_dir / f"{number}_translated.txt"
    zh_text = _read_text(zh_path) if zh_path.exists() else ""
    en_text = _read_text(en_path) if en_path.exists() else ""

    zh_paras = split_paragraphs(zh_text)
    en_paras = split_paragraphs(en_text)
    rows = _align.align_cached(zh_path, en_path, zh_paras, en_paras)

    glossary = _read_json(work_dir / f"glossary_{number}.json", {}) or {}
    refine = _read_json(work_dir / f"{number}_refine.json", {}) or {}
    conflicts = [
        c for c in read_conflicts(work_dir)
        if c.get("origin_chapter") == number or c.get("chapter") == number
    ]

    # 批注按行号挂到对齐行上；只有当段落内容仍与批注记录一致时才显示，
    # 否则说明译文后来又被改过，旧理由已经过时。
    notes_by_row = {}
    for note in (refine.get("notes") or []):
        if not isinstance(note, dict):
            continue
        try:
            notes_by_row[int(note.get("row"))] = note
        except (TypeError, ValueError):
            continue

    def flatten(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    for index, row in enumerate(rows, start=1):
        note = notes_by_row.get(index)
        row["note"] = None
        if not note:
            continue
        after = flatten(note.get("after"))
        current = flatten("\n".join(row.get("en_parts") or []))
        if not after or not current:
            continue
        # 一次修订可能覆盖多段，重新对齐后这些段落会被分到相邻行里，
        # 所以除了完全相等，也接受"当前行是修订文本的首/尾片段"。
        matched = after == current or (
            len(current) >= 12 and (after.startswith(current) or after.endswith(current))
        )
        if matched:
            row["note"] = {
                "reason": note.get("reason") or "",
                "changed": bool(note.get("changed")),
            }

    return {
        "num": number,
        "has_origin": zh_path.exists(),
        "has_translated": en_path.exists(),
        "terms": chapter_terms(work_dir, zh_text, glossary),
        "refine_updated": refine.get("updated") or "",
        "refine_count": len(notes_by_row),
        "zh_paragraphs": len(zh_paras),
        "en_paragraphs": len(en_paras),
        "zh_chars": _cjk_count(zh_text),
        "en_words": _word_count(en_text),
        "rows": rows,
        "glossary": glossary,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# 术语库 / 冲突
# ---------------------------------------------------------------------------

def read_glossary(work_dir: Path, number: int) -> Dict[str, Any]:
    return _read_json(work_dir / f"glossary_{number}.json", {}) or {}


_TRACKER_CACHE: Dict[str, Any] = {}


def read_tracker(work_dir: Path) -> Dict[str, Any]:
    """读取术语总表（按 mtime 缓存：文件几十 KB，逐章都读会很浪费）。"""
    path = work_dir / "global_glossary_tracker.json"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path)
    cached = _TRACKER_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    data = _read_json(path, {}) or {}
    _TRACKER_CACHE[key] = (stamp, data)
    return data


def _split_renderings(value: Any) -> List[str]:
    """把一个术语的译法拆成若干条候选（同一术语可能有多个译法）。

    约定用 " / " 分隔多个译法，例如 "Special Effect / Effect"，两条都要能高亮。
    过长的值多半是解释而非译法，直接丢弃，免得正文被刷满。
    """
    raw: List[str] = []
    if isinstance(value, dict):
        raw = [str(v) for v in value.values() if v]
    elif isinstance(value, str):
        raw = [value]
    out: List[str] = []
    for item in raw:
        for piece in str(item).split(" / "):
            piece = piece.strip().strip('"').strip("'")
            if not piece or _CJK_RE.search(piece):
                continue
            if len(piece) > 60 or len(piece.split()) > 8:
                continue
            if piece not in out:
                out.append(piece)
    return out


def chapter_terms(work_dir: Path, chapter_text: str, chapter_glossary: Dict[str, Any],
                  *, limit: int = 400) -> List[Dict[str, Any]]:
    """列出"在本章正文里出现"的术语，供前端做中英双向高亮。

    来源有两处，本章新术语优先，其次是全书总表。只返回真的出现在正文里的词。
    每条同时给出 translations（拆好的英文译法列表），前端据此把英文一侧也标出来。
    """
    tracker = read_tracker(work_dir)
    ordered: List[tuple] = []
    seen = set()

    def add(term: Any, value: Any, category: str, priority: int) -> None:
        term = str(term or "").strip()
        if not term or term in seen or not _CJK_RE.search(term):
            return
        if term not in chapter_text:
            return
        translations = _split_renderings(value)
        seen.add(term)
        ordered.append((priority, -len(term), term, translations, category))

    for category in ("fixed_terms", "contextual_terms"):
        for term, value in (chapter_glossary.get(category) or {}).items():
            add(term, value, category, 0)
    for category in ("fixed_terms", "contextual_terms"):
        for term, info in (tracker.get(category) or {}).items():
            add(term, info.get("value") if isinstance(info, dict) else info, category, 1)

    ordered.sort(key=lambda item: (item[0], item[1]))
    return [{"term": term, "translation": " / ".join(translations),
             "translations": translations, "category": category}
            for _, _, term, translations, category in ordered[:limit]]


def glossary_categories() -> tuple:
    """术语库的全部类别（与 novelkit.glossary.CATEGORIES 保持一致）。"""
    from novelkit import glossary as nkglossary

    return tuple(nkglossary.CATEGORIES)


def read_global_glossary(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取全局术语库（所有作品共用，自动流程永不覆盖）。"""
    return _read_json(Path(path) if path else global_glossary_path(), {}) or {}


def read_conflicts(work_dir: Path) -> List[Dict[str, Any]]:
    path = work_dir / "glossary_conflicts.jsonl"
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records


def search_chapters(work_dir: Path, query: str, limit: int = 60) -> List[Dict[str, Any]]:
    """在原文与译文中做简单子串检索，返回命中的章节与片段。"""
    query = (query or "").strip()
    if not query:
        return []
    lowered = query.lower()
    hits: List[Dict[str, Any]] = []

    numbers: List[int] = []
    for name in os.listdir(work_dir):
        match = _ORIGIN_RE.match(name)
        if match:
            numbers.append(int(match.group(1)))

    for number in sorted(numbers):
        if len(hits) >= limit:
            break
        zh = _read_text(work_dir / f"{number}_origin.txt")
        en = _read_text(work_dir / f"{number}_translated.txt")
        where: List[str] = []
        snippet = ""
        if lowered in zh.lower():
            where.append("zh")
            pos = zh.lower().find(lowered)
            snippet = zh[max(0, pos - 30): pos + len(query) + 30].replace("\n", " ")
        if lowered in en.lower():
            where.append("en")
            if not snippet:
                pos = en.lower().find(lowered)
                snippet = en[max(0, pos - 40): pos + len(query) + 40].replace("\n", " ")
        if where:
            hits.append({"num": number, "in": where, "snippet": snippet})
    return hits
