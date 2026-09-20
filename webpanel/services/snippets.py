"""使用者补充要求（<user_supplement>）的持久化。

三种场合各有独立的一份，互不干扰：

    translate     整章翻译
    retranslate   局部重译（只重译选中段落）
    refine        点评与修订（AI 给理由）

**为什么存在服务端而不是浏览器 localStorage**：这类要求往往带着作品的语气、
译名偏好等长期约定，属于项目资产，应该跟着工作区走（换浏览器、清缓存都不该丢），
也方便直接用编辑器改。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNIPPETS_PATH = _PROJECT_ROOT / "webpanel" / "prompt_snippets.json"

KINDS: List[Dict[str, str]] = [
    {
        "id": "translate",
        "label": "整章翻译",
        "placeholder": "例：本章战斗场面要短促有力，多用动词开头的短句；避免使用被动语态。",
    },
    {
        "id": "retranslate",
        "label": "局部重译",
        "placeholder": "例：这几段是在吵架，语气要更冲，可以用破折号打断句子。",
    },
    {
        "id": "refine",
        "label": "点评与修订",
        "placeholder": "例：重点检查时态是否统一、英文是否地道；改动尽量小，不要重写整段。",
    },
]

_VALID = {item["id"] for item in KINDS}


def _rel(path: Path) -> str:
    """相对项目根显示；配置被指到项目外（测试）时退回绝对路径，不要抛异常。"""
    try:
        return str(path.relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)
MAX_LENGTH = 8000


def _load() -> Dict[str, Dict[str, str]]:
    try:
        with SNIPPETS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {
                str(work): {str(k): str(v) for k, v in kinds.items() if v}
                for work, kinds in data.items()
                if isinstance(kinds, dict)
            }
    except (OSError, ValueError):
        pass
    return {}


def _save(data: Dict[str, Dict[str, str]]) -> Path:
    SNIPPETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".snippets_", dir=str(SNIPPETS_PATH.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, SNIPPETS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return SNIPPETS_PATH


def get_all(work: str) -> Dict[str, Any]:
    stored = _load().get(work, {})
    return {
        "ok": True,
        "work": work,
        "kinds": KINDS,
        "snippets": {item["id"]: stored.get(item["id"], "") for item in KINDS},
        "path": _rel(SNIPPETS_PATH),
    }


def get(work: str, kind: str) -> str:
    return _load().get(work, {}).get(kind, "")


def save(work: str, kind: str, text: str) -> Dict[str, Any]:
    if kind not in _VALID:
        return {"ok": False, "error": f"未知的提示词类型: {kind}"}
    text = (text or "").replace("\r\n", "\n")
    if len(text) > MAX_LENGTH:
        return {"ok": False, "error": f"内容过长（{len(text)} 字符，上限 {MAX_LENGTH}）"}

    data = _load()
    bucket = data.setdefault(work, {})
    if text.strip():
        bucket[kind] = text
    else:
        bucket.pop(kind, None)
    if not bucket:
        data.pop(work, None)
    path = _save(data)
    return {"ok": True, "work": work, "kind": kind,
            "path": _rel(path),
            "snippets": {item["id"]: data.get(work, {}).get(item["id"], "") for item in KINDS}}


def materialise(work: str, kind: str) -> str:
    """把补充要求落成临时文件，返回路径；没有内容则返回空串。

    走文件而不是命令行参数：提示词可能很长、含换行与引号，
    塞进 argv 容易被 shell 转义搞坏。
    """
    text = get(work, kind)
    if not text.strip():
        return ""
    target_dir = _PROJECT_ROOT / "log" / "panel_jobs"
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"instr_{kind}_", suffix=".txt", dir=str(target_dir))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
