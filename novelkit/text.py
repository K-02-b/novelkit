"""文本工具：章节号解析、标点规范化、中文/Emoji 检测、原子读写。

这些函数被 scripts/ 下的翻译/校对/重译/精修入口共同复用，
以保证所有脚本对"章节号写法"和"标点处理"的理解完全一致。
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from typing import Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# 正则
# --------------------------------------------------------------------------

# 中日韩统一表意文字（含扩展 A 与兼容区）
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Emoji / 图形符号 / 变体选择符 / 零宽连接符
EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 各类 Emoji 区块
    "\U00002600-\U000027bf"  # 杂项符号与装饰符号
    "\U0001f1e6-\U0001f1ff"  # 区域指示符（国旗）
    "\u2190-\u21ff"          # 箭头
    "\u2b00-\u2bff"          # 杂项符号与箭头
    "\u20e3"                 # 组合包围键帽
    "\ufe0f"                 # 变体选择符-16
    "\u200d"                 # 零宽连接符
    "]"
)

# 章节号分隔符：空白、逗号、顿号、中文括号、方括号都视作分隔
_CHAPTER_SEPARATORS = str.maketrans({c: " " for c in "，,、;；【】[]（）()|"})
_RANGE_SEP_RE = re.compile(r"^\s*(\d+)\s*[-~—–]\s*(\d+)\s*$")
_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")

# 全角 ASCII（！ 到 ～）→ 半角
_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # 全角空格

# 中文标点 → 英文标点。注意：这里只处理"翻译完成后仍可能残留"的字符。
_PUNCT_MAP = str.maketrans(
    {
        "，": ", ",
        "、": ", ",
        "。": ". ",
        "？": "? ",
        "！": "! ",
        "“": '"',
        "”": '"',
        "《": "<",
        "》": ">",
        "【": "[",
        "】": "]",
        "～": "~",
        "•": "·",
        "；": "; ",
        "：": ": ",
        "…": "...",
        "‘": "'",
        "’": "'",
        "\u3000": " ",
        "\u00a0": " ",
    }
)

_ASTERISK_MAP = str.maketrans({"*": ""})

# 新建文件时的默认权限，等价于旧版 open(...,'w') 受 umask 影响的结果
_UMASK = os.umask(0)
os.umask(_UMASK)
_DEFAULT_FILE_MODE = 0o666 & ~_UMASK


# --------------------------------------------------------------------------
# 章节号解析
# --------------------------------------------------------------------------

def parse_chapters(spec: Optional[str], *, allow_all: bool = False) -> Optional[List[int]]:
    """把章节号表达式解析为升序去重列表。

    同时兼容两套历史写法（旧 main.py 用空格、旧 checker.py 用逗号）：
        "1-4 6 8-9"   "1,3,5-10"   "【1-4】"   "10,15,17,19"
        "1-4, 6"      "1 - 3"

    allow_all=True 时，"all"/"全部"/空串返回 None（表示不筛选）。
    解析失败抛出 ValueError，并指出出错的那一段。
    """
    if spec is None:
        return None if allow_all else []

    cleaned = str(spec).translate(_CHAPTER_SEPARATORS).strip()
    if not cleaned:
        return None if allow_all else []

    if allow_all and cleaned.lower() in {"all", "全部", "*"}:
        return None

    chapters: set[int] = set()
    for token in cleaned.split():
        m = _RANGE_SEP_RE.match(token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if end < start:
                start, end = end, start
            chapters.update(range(start, end + 1))
            continue
        m = _SINGLE_RE.match(token)
        if m:
            chapters.add(int(m.group(1)))
            continue
        raise ValueError(f"无法解析章节号片段: {token!r}（完整输入: {spec!r}）")

    return sorted(chapters)


def is_chapter_selected(chapter: int, selection: Optional[Iterable[int]]) -> bool:
    """selection 为 None 表示全选。"""
    return selection is None or chapter in set(selection)


# --------------------------------------------------------------------------
# 标点 / 文本规范化
# --------------------------------------------------------------------------

def normalize_punctuation(content: str, *, strip_asterisk: bool = True) -> str:
    """把译文里残留的中文标点、全角字符规范化为英文半角。

    strip_asterisk=True 时删除 Markdown 强调符 '*'（沿用旧 main.py 行为，
    因为提示词明确要求正文禁止 Markdown 修饰）。
    """
    if not content:
        return content
    content = content.translate(_FULLWIDTH_MAP)
    content = content.translate(_PUNCT_MAP)
    if strip_asterisk:
        content = content.translate(_ASTERISK_MAP)
    return content


def detect_issues(content: str) -> dict:
    """检测译文中残留的中文与 Emoji，返回计数与行号（便于定位）。"""
    cjk_lines: List[int] = []
    cjk_count = 0
    emoji_lines: List[int] = []
    emoji_count = 0

    for idx, line in enumerate(content.splitlines(), start=1):
        cjk_hits = CJK_RE.findall(line)
        if cjk_hits:
            cjk_count += len(cjk_hits)
            cjk_lines.append(idx)
        emoji_hits = EMOJI_RE.findall(line)
        if emoji_hits:
            emoji_count += len(emoji_hits)
            emoji_lines.append(idx)

    return {
        "has_cjk": cjk_count > 0,
        "cjk_count": cjk_count,
        "cjk_lines": cjk_lines,
        "has_emoji": emoji_count > 0,
        "emoji_count": emoji_count,
        "emoji_lines": emoji_lines,
    }


_ZH_CHAPTER_RE = re.compile(r"^\s*第\s*(\d+)\s*章")
_EN_CHAPTER_RE = re.compile(r"^Chapter\s+(\d+)\s*:\s*\S")


def check_chapter_heading(source: str, translated: str) -> str:
    """校验"首行章节标题"的格式，返回问题描述（没有问题时返回空串）。

    全书标题必须统一成 `Chapter NN: Title`。模型偶尔会把冒号漏掉
    （实测出过 `Chapter 02 Night Chase` 这种漏冒号的写法），
    更糟的是把标题整行吞掉、直接以正文开头。这两种都不该静默写进文件。
    """
    source_lines = [line for line in (source or "").splitlines() if line.strip()]
    translated_lines = [line for line in (translated or "").splitlines() if line.strip()]
    if not source_lines or not translated_lines:
        return ""

    match = _ZH_CHAPTER_RE.match(source_lines[0])
    if not match:
        return ""                      # 没有章节标题（简介、序章等）不参与校验
    number = int(match.group(1))
    heading = translated_lines[0]

    found = _EN_CHAPTER_RE.match(heading)
    if not found:
        lowered = heading.lower()
        if "chapter" in lowered:
            return (f"首行标题格式不合规：{heading!r}；"
                    f"应为 'Chapter {number:02d}: <标题>'（冒号不能省）")
        return (f"首行不是标题（疑似标题被漏译）：{heading[:40]!r}；"
                f"原文标题为 {source_lines[0]!r}")
    if int(found.group(1)) != number:
        return f"首行标题章节号不符：{heading!r}，原文是第 {number} 章"
    return ""


def has_cjk(content: str) -> bool:    return bool(CJK_RE.search(content or ""))


def count_units(content: str, unit: str) -> int:
    """按 unit（words/chars）统计长度。chars 以字符计，words 以空白切分计。"""
    if not content:
        return 0
    if unit == "chars":
        return len(content)
    return len(content.split())


def take_tail(content: str, amount: int, unit: str) -> str:
    """取文本末尾 amount 个 unit。"""
    if not content or amount <= 0:
        return ""
    if unit == "chars":
        return content[-amount:] if len(content) > amount else content
    words = content.split()
    if len(words) <= amount:
        return content.strip()
    return " ".join(words[-amount:])


def take_head(content: str, amount: int, unit: str) -> str:
    """取文本开头 amount 个 unit。"""
    if not content or amount <= 0:
        return ""
    if unit == "chars":
        return content[:amount] if len(content) > amount else content
    words = content.split()
    if len(words) <= amount:
        return content.strip()
    return " ".join(words[:amount])


# --------------------------------------------------------------------------
# 文件读写
# --------------------------------------------------------------------------

def read_text(path: str, default: Optional[str] = None) -> Optional[str]:
    """读取文本；文件不存在时返回 default（而不是抛异常）。"""
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_text_atomic(path: str, content: str) -> None:
    """原子写入：先写临时文件再 os.replace，避免中途崩溃留下半截文件。

    这一点很重要——旧版直接 'w' 覆盖，一旦翻译过程中断，
    已完成的章节文件会被截断成空文件，重跑时被当成"已翻译"而跳过。

    另外要修掉 tempfile.mkstemp 的副作用：它创建的文件是 0600，
    直接 replace 会把所有译文/术语库从 0644 变成 0600。
    这里显式恢复权限：覆盖已有文件时沿用原权限，新建文件用 umask 默认值。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = _DEFAULT_FILE_MODE

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: str, data, *, indent: int = 2, sort_keys: bool = False) -> None:
    """原子写入 JSON（UTF-8，不转义非 ASCII）。"""
    import json

    payload = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    write_text_atomic(path, payload)


def safe_int(text: str, default: Optional[int] = None) -> Optional[int]:
    """从字符串里提取第一个整数（用于 '12_origin.txt' 这类文件名）。"""
    if text is None:
        return default
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else default


def chapter_files(work_dir: str, suffix: str = "_origin.txt") -> List[int]:
    """列出目录下所有形如 '<n>_origin.txt' 的章节号，升序返回。"""
    if not os.path.isdir(work_dir):
        return []
    numbers = []
    for name in os.listdir(work_dir):
        if not name.endswith(suffix):
            continue
        num = safe_int(name[: -len(suffix)])
        if num is not None:
            numbers.append(num)
    return sorted(numbers)


def format_chapter_list(chapters: Sequence[int], limit: int = 12) -> str:
    """把章节列表压缩成 '1-4, 8, 10-12' 这样的可读字符串。"""
    if not chapters:
        return "(空)"
    ordered = sorted(set(chapters))
    ranges: List[str] = []
    start = prev = ordered[0]
    for num in ordered[1:]:
        if num == prev + 1:
            prev = num
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = num
    ranges.append(str(start) if start == prev else f"{start}-{prev}")

    if len(ranges) > limit:
        shown = ", ".join(ranges[:limit])
        return f"{shown}, ... 共 {len(ordered)} 章"
    return ", ".join(ranges)
