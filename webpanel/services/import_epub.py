"""EPUB 导入：先扫描列出章节（可预览、可勾选），再按选择写入作品目录。

与 scripts/split_epub.py 的关系
-------------------------------
split_epub.py 是"一次性全量拆分"，把 EPUB 内部条目按固定下标硬编码（简介=items[1]，
正文=items[3:]）。导入面板需要更灵活：不同 EPUB 的封面/版权页/目录页数量不一样，
硬编码很容易错位。所以这里：

* 扫描时列出**全部**文档条目（下标、标题、字数、预览），并给出建议值；
* 由用户确认哪一个是简介、正文从哪个下标开始、导入哪些章节；
* 默认全选、默认包含简介，与 split_epub.py 的默认行为一致；
* 写入前会把目标目录已有文件的情况报出来，避免误覆盖。

依赖 ebooklib + beautifulsoup4（在 requirements.txt 里，需用 .venv/bin/python 启动面板；
面板的其它视图不依赖它们）。
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from novelkit import config as nkconfig  # noqa: E402

# 作品写到哪里：工作区根目录（works/，可用 NOVELKIT_WORKSPACE 覆盖）。
_WORKSPACE_ROOT = nkconfig.workspace_root()

# scan_id -> {"path": 临时文件, "items": [...]}
_SCANS: Dict[str, Dict[str, Any]] = {}
_MAX_SCANS = 8
_PREVIEW_CHARS = 320


class EpubError(RuntimeError):
    pass


def _require_deps() -> None:
    try:
        import ebooklib  # noqa: F401
        import bs4  # noqa: F401
    except ImportError as exc:
        raise EpubError(
            "缺少依赖 ebooklib / beautifulsoup4，请用 .venv/bin/python 启动面板，"
            "或执行：.venv/bin/pip install EbookLib beautifulsoup4"
        ) from exc


def _clean_text(html_bytes: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _guess_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return fallback


# --- 条目分类：只认"目录/导航文档" -------------------------------------------
# 不再猜"什么标题是番外/后记/封面"：各种写法太多，猜错比不猜更糟。
# 其它条目一律按"非空即可当正文"对待，由用户在表里自己勾。

# "第01章 免费角色"、"第1章"、"第 12 章 标题"；也接受阿拉伯数字标题（"1. 起点"、"2、"）
_CHAPTER_RE = re.compile(r"^\s*第\s*(\d+)\s*[章回节篇]")
_ARABIC_CHAPTER_RE = re.compile(r"^\s*(\d{1,4})\s*(?:[.、,，:：]|\s)\s*\S")
_BARE_NUMBER_RE = re.compile(r"^\s*(\d{1,4})\s*$")
# 目录/导航文档（文件名或标题）
_TOC_TEXT_RE = re.compile(r"^(目录|目\s*录|contents?|table\s+of\s+contents|toc)$", re.I)
_TOC_NAME_RE = re.compile(r"(^|[^a-z])(toc|nav|contents?)([^a-z]|$)", re.I)


def _plain(text: str) -> str:
    """去掉空白与常见分隔符，便于比较"目录"这类短标题。"""
    return re.sub(r"[\s·・._\-—]+", "", (text or "").strip())


def _is_toc(item: Dict[str, Any]) -> bool:
    title = item.get("title") or ""
    name = Path(item.get("name") or "").stem
    if _TOC_TEXT_RE.match(_plain(title)):
        return True
    return bool(_TOC_NAME_RE.search(name))


def chapter_number(item: Dict[str, Any]) -> Optional[int]:
    """条目对应的章节号；解析不出来返回 None（目录、封面、简介等）。"""
    if item.get("empty") or _is_toc(item):
        return None
    for value in (item.get("title"), Path(item.get("name") or "").stem):
        if not value:
            continue
        text = str(value).strip()
        match = _CHAPTER_RE.match(text) or _ARABIC_CHAPTER_RE.match(text) or _BARE_NUMBER_RE.match(text)
        if match:
            return int(match.group(1))
    return None


def scan_bytes(data: bytes, filename: str = "upload.epub") -> Dict[str, Any]:
    """解析上传的 EPUB，返回条目清单（不写任何作品文件）。"""
    _require_deps()
    if not data:
        raise EpubError("上传内容为空")

    import ebooklib
    from ebooklib import epub

    tmp_dir = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    tmp_path = tmp_dir / (os.path.basename(filename) or "upload.epub")
    tmp_path.write_bytes(data)

    try:
        book = epub.read_epub(str(tmp_path))
    except Exception as exc:  # noqa: BLE001
        raise EpubError(f"无法解析 EPUB：{type(exc).__name__}: {exc}") from exc

    documents = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]

    items: List[Dict[str, Any]] = []
    for index, item in enumerate(documents):
        try:
            raw = item.get_content()
        except Exception:  # noqa: BLE001
            continue
        text = _clean_text(raw)
        entry = {
            "index": index,
            "id": item.get_id() or f"item{index}",
            "name": os.path.basename(item.get_name() or ""),
            "title": _guess_title(text, f"（第 {index} 个条目，无标题）"),
            "chars": len(text),
            "paragraphs": len([line for line in text.splitlines() if line.strip()]),
            "preview": text[:_PREVIEW_CHARS],
            "empty": not text.strip(),
        }
        entry["toc"] = _is_toc(entry)
        entry["chapter_number"] = chapter_number(entry)
        items.append(entry)

    if not items:
        raise EpubError("该 EPUB 里没有可用的文档条目")

    usable = [it for it in items if not it["empty"] and not it["toc"]]

    # 不做"特殊标题"猜测（封面/版权/番外/后记…各种写法太多，猜错比不猜更糟）。
    # 简介只按位置建议：第一个**编号章节**之前、非目录的最后一条 —— 通常就是"简介"。
    numbered = [it for it in usable if it["chapter_number"] is not None]
    first_numbered = numbered[0]["index"] if numbered else None
    if first_numbered is None:
        suggested_intro = None            # 没有编号章节可参照，就别擅自指认简介
    else:
        before = [it["index"] for it in usable if it["index"] < first_numbered]
        suggested_intro = before[-1] if before else None
    suggested_body_start = first_numbered if first_numbered is not None else (
        usable[0]["index"] if usable else items[0]["index"])

    scan_id = uuid.uuid4().hex[:12]
    if len(_SCANS) >= _MAX_SCANS:
        old_id, old = _SCANS.pop(next(iter(_SCANS)))
        try:
            old["path"].unlink()
            old["path"].parent.rmdir()
        except OSError:
            pass
    _SCANS[scan_id] = {"path": tmp_path, "items": items}

    return {
        "ok": True,
        "scan_id": scan_id,
        "filename": filename,
        "bytes": len(data),
        "items": items,
        "suggested_intro": suggested_intro,
        "suggested_body_start": suggested_body_start,
        "document_count": len(items),
    }


def preview(scan_id: str, index: int, *, chars: int = 2000) -> Dict[str, Any]:
    entry = _SCANS.get(scan_id)
    if entry is None:
        return {"ok": False, "error": "扫描结果已过期，请重新上传"}
    for item in entry["items"]:
        if item["index"] == index:
            return {"ok": True, "index": index, "title": item["title"],
                    "text": item["preview"][:chars] if chars <= _PREVIEW_CHARS else item["preview"]}
    return {"ok": False, "error": "条目不存在"}


_WORK_NAME_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def _valid_work_name(name: str) -> bool:
    """作品名校验：与「编辑 → 重命名作品」共用同一套规则（见 services.works）。"""
    from services.works import valid_work_name

    return valid_work_name(name)


def commit(scan_id: str, work: str, *, include_intro: bool, intro_index: Optional[int],
           body_indices: List[int], start_number: int = 1, overwrite: bool = False) -> Dict[str, Any]:
    """把选中的条目写入作品目录：简介 -> 0_origin.txt，正文 -> N_origin.txt。"""
    entry = _SCANS.get(scan_id)
    if entry is None:
        return {"ok": False, "error": "扫描结果已过期，请重新上传"}

    if not _valid_work_name(work):
        return {"ok": False,
                "error": "作品名里不能包含 / \\ 或其它特殊字符，也不能是 . 或 .."}
    work_dir = (_WORKSPACE_ROOT / work).resolve()
    try:
        work_dir.relative_to(_WORKSPACE_ROOT.resolve())
    except ValueError:
        return {"ok": False, "error": "非法的作品目录"}

    items_by_index = {item["index"]: item for item in entry["items"]}
    intro_idx = intro_index if (include_intro and intro_index is not None) else None
    # 简介只以 0_origin.txt 的形式导入一次：前端会把它变灰，但接口也不能重复写
    skipped_intro = intro_idx is not None and intro_idx in body_indices
    selected = [items_by_index[i] for i in body_indices
                if i in items_by_index and i != intro_idx]
    if not selected:
        return {"ok": False, "error": "没有选中任何正文章节"}

    # 重新从 EPUB 取正文（preview 只有前 320 字）
    texts = _extract_texts(entry["path"], {it["index"] for it in selected}
                           | ({intro_index} if include_intro and intro_index is not None else set()))
    if texts is None:
        return {"ok": False, "error": "EPUB 临时文件已失效，请重新上传"}

    work_dir.mkdir(parents=True, exist_ok=True)

    planned: List[Dict[str, Any]] = []
    if intro_idx is not None and intro_idx in texts:
        planned.append({"index": intro_idx, "number": 0,
                        "title": items_by_index.get(intro_idx, {}).get("title", "简介"),
                        "chars": len(texts[intro_idx])})
    for offset, item in enumerate(selected):
        planned.append({"index": item["index"], "number": start_number + offset,
                        "title": item["title"], "chars": len(texts.get(item["index"], ""))})

    existing = [p["number"] for p in planned
                if (work_dir / f"{p['number']}_origin.txt").exists()]
    if existing and not overwrite:
        return {"ok": False,
                "error": f"目标目录已有 {len(existing)} 个同名文件（如 "
                         f"{existing[:5]}），如确认覆盖请勾选「覆盖已有文件」",
                "existing": existing}

    written: List[str] = []
    for plan in planned:
        text = texts.get(plan["index"], "")
        if not text.strip():
            continue
        path = work_dir / f"{plan['number']}_origin.txt"
        path.write_text(text, encoding="utf-8")
        written.append(path.name)

    # 写入的编号永远是连续的（1..N），所以"编号缺口"没有意义；
    # 真正会漏东西的是**原书章节号**不连续：勾选时漏掉了某一章。
    source_numbers = sorted(it["chapter_number"] for it in selected
                            if it.get("chapter_number") is not None)
    missing_chapters = [n for n in range(source_numbers[0], source_numbers[-1] + 1)
                        if n not in source_numbers] if len(source_numbers) > 1 else []
    return {
        "ok": True,
        "work": work,
        "written": written,
        "count": len(written),
        "planned": planned,
        "skipped_intro": skipped_intro,
        "missing_chapters": missing_chapters,
        "existing_overwritten": existing if overwrite else [],
    }


def _extract_texts(epub_path: Path, indices: set) -> Optional[Dict[int, str]]:
    try:
        _require_deps()
        import ebooklib
        from ebooklib import epub
        book = epub.read_epub(str(epub_path))
        documents = [i for i in book.get_items() if i.get_type() == ebooklib.ITEM_DOCUMENT]
        result: Dict[int, str] = {}
        for index, item in enumerate(documents):
            if index not in indices:
                continue
            result[index] = _clean_text(item.get_content())
        return result
    except Exception:  # noqa: BLE001
        return None


def decode_base64(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise EpubError(f"base64 解码失败: {exc}") from exc
