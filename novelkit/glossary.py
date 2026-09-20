"""术语库：读取、合并、权威译法登记、冲突拦截、全局 tracker。

术语优先级（本次重写的核心）
--------------------------
    全局 glossary.json（人工维护，最高权威）
        > 最早记录该词义的章节（先到先得，一经确立不可被后续章节改写）
        > 后续章节

两条关键规则：
  1. **全局优先**：`glossary.json` 是人工整理的基准，任何章节都不能覆盖它。
     （旧版反过来——章节术语覆盖全局术语，AI 的幻觉可以顶掉人工译法。）
  2. **先到先得**：同一 (类别, 词, 语境) 只认第一次出现的译法。
     （旧版 contextual_terms 是"后来的语境覆盖先前的"，导致第 5 章能把第 0 章
      已定的译法改掉，而第 0~4 章的正文早已发布——这正是"新译文违背旧正文"的根源。）

写入时强制
----------
模型即使被反复叮嘱"不要重复输出已有术语"，实际仍会把已定词条再吐一遍
（真实数据里 two/ 有 3 个 fixed_terms、4 个 contextual 冲突，air/ 有 3 个）。
因此不能只靠提示词约束，必须在落盘前拦截：

    sanitize_chapter()  把与既定译法冲突的重复输出丢弃（或按 --allow-term-changes 放行），
                        并生成冲突记录；只有真正的新词才会写进 glossary_<n>.json。

这样 `glossary_<n>.json` 始终等于"第 n 章新确立的术语"，先到先得的不变量
不依赖任何读取顺序就天然成立。

性能：文件读取仍是 O(n)（带 mtime 失效的章节缓存），另有一个可忽略的
内存级重建开销。
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import text as nktext

CATEGORIES = ("fixed_terms", "contextual_terms", "aesthetic_sentences", "cultural_nuances")

# 术语库的权威哨兵：数值越小优先级越高（先到先得＝先应用者胜）
#   AUTHORITY_WORK   作品级人工基准（<作品目录>/glossary.json）——最高
#   AUTHORITY_GLOBAL 全局人工基准（工作区根 glossary.json）——跨作品共用
#   0,1,2...         章节术语库，按章节先后
AUTHORITY_WORK = -2
AUTHORITY_GLOBAL = -1

# 作品级术语库文件名（注意：与章级的 glossary_<n>.json 不冲突，
# existing_chapters() 的正则要求下划线加数字）
WORK_GLOSSARY_FILENAME = "glossary.json"

CONFLICTS_FILENAME = "glossary_conflicts.jsonl"


def empty_glossary() -> Dict[str, Dict[str, Any]]:
    return {category: {} for category in CATEGORIES}


def _normalise(glossary: Any) -> Dict[str, Dict[str, Any]]:
    """保证四个类别都存在且都是 dict。"""
    result = empty_glossary()
    if isinstance(glossary, dict):
        for category in CATEGORIES:
            value = glossary.get(category)
            if isinstance(value, dict):
                result[category] = value
    return result


def load_glossary_file(path: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """读取术语库文件；不存在或损坏时返回 None（由调用方决定如何提示）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _normalise(json.load(handle))
    except (json.JSONDecodeError, OSError):
        return None


def compare_translation(old: str, new: str) -> str:
    """比较两个译法：same（完全一致）/ minor（仅大小写或空白差异）/ different。"""
    if old == new:
        return "same"
    if old.strip().lower() == new.strip().lower():
        return "minor"
    if re.sub(r"\s+", " ", old).strip().lower() == re.sub(r"\s+", " ", new).strip().lower():
        return "minor"
    return "different"


def _merge_into(base: Dict, incoming: Dict) -> Dict[str, Dict[str, Any]]:
    """把 incoming 并入 base：同名键以 incoming 为准，base 独有的键保留。"""
    result = _normalise(base)
    for category in CATEGORIES:
        for key, value in (incoming or {}).get(category, {}).items():
            if category == "contextual_terms":
                if not isinstance(value, dict):
                    continue
                bucket = result[category].setdefault(key, {})
                if not isinstance(bucket, dict):
                    bucket = result[category][key] = {}
                bucket.update(value)
            else:
                result[category][key] = value
    return result


def merge_glossaries(global_glos: Dict, local_glos: Dict) -> Dict[str, Dict[str, Any]]:
    """合并全局与章节术语库。**全局优先**，且同一语境只保留先出现的译法。

    与旧版的关键差异：旧版让 local 覆盖 global，且 contextual 逐个语境用
    `dict.update()` 覆盖；新版统一为 setdefault 语义。
    """
    merged = _normalise(global_glos)

    for category in CATEGORIES:
        for key, value in (local_glos or {}).get(category, {}).items():
            if category == "contextual_terms":
                if not isinstance(value, dict):
                    continue
                target = merged[category].setdefault(key, {})
                if not isinstance(target, dict):
                    merged[category][key] = dict(value)
                    continue
                for ctx_key, ctx_val in value.items():
                    target.setdefault(ctx_key, ctx_val)
            else:
                merged[category].setdefault(key, value)
    return merged


def build_chapter_glossary(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """从模型返回的 payload 构造本章 glossary_<n>.json 内容（未净化）。"""
    return {
        "fixed_terms": dict(payload.get("new_fixed_terms") or {}),
        "contextual_terms": dict(payload.get("new_contextual_terms") or {}),
        "aesthetic_sentences": dict(payload.get("new_aesthetic_sentences") or {}),
        "cultural_nuances": dict(payload.get("new_cultural_nuances") or {}),
    }


class GlossaryStore:
    """带缓存的章节术语库仓库 + 权威译法登记表。

    典型用法：
        store = GlossaryStore(work_dir, global_glossary_path)
        merged = store.merged_up_to(target_num)          # 全局 + 第 0..target-1 章
        relevant = store.filter_for_content(merged, content, future)
        ... 模型返回 payload ...
        chapter_glos = build_chapter_glossary(payload)
        clean, conflicts = store.sanitize_chapter(target_num, chapter_glos)
        store.save_chapter(target_num, clean)
        store.record_conflicts(conflicts)
        store.write_tracker()
    """

    _matcher_cache: Dict[str, Optional[re.Pattern]] = {}

    def __init__(self, work_dir: str, global_glossary_path: Optional[str] = None):
        self.work_dir = work_dir
        self.global_glossary_path = global_glossary_path
        self.warnings: List[str] = []

        self.global_glossary: Dict[str, Dict[str, Any]] = empty_glossary()
        if global_glossary_path:
            loaded = load_glossary_file(global_glossary_path)
            if loaded is not None:
                self.global_glossary = loaded

        # 作品级人工术语库：只对这一本书生效，优先级高于全局与所有章节。
        # 用途是"原著术语/关键设定"——它们往往只在这本书里有意义，
        # 放进跨作品共用的全局 glossary.json 会污染别的作品。
        self.work_glossary_path = os.path.join(work_dir, WORK_GLOSSARY_FILENAME)
        self.work_glossary: Dict[str, Dict[str, Any]] = empty_glossary()
        loaded_work = load_glossary_file(self.work_glossary_path)
        if loaded_work is not None:
            self.work_glossary = loaded_work

        self._cache: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._file_keys: Dict[int, Tuple[float, int]] = {}
        self._merged: Optional[Dict[str, Dict[str, Any]]] = None
        self._merged_upto: int = -1
        # (category, term, context|None) -> (首次登记章节, 权威译法)
        self._estab: Dict[Tuple[str, str, Optional[str]], Tuple[int, str]] = {}

    # ---------------------------------------------------------------- 缓存

    def _stat_key(self, path: str) -> Optional[Tuple[float, int]]:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def chapter(self, number: int, *, use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
        """读取单章术语库（带 mtime+size 失效的缓存）。"""
        path = os.path.join(self.work_dir, f"glossary_{number}.json")
        if use_cache:
            key = self._stat_key(path)
            if key is not None and self._file_keys.get(number) == key and number in self._cache:
                return self._cache[number]
            if key is None and not os.path.exists(path):
                self._cache.setdefault(number, empty_glossary())
                return self._cache[number]

        data = load_glossary_file(path)
        if data is None:
            if os.path.exists(path):
                self.warnings.append(f"glossary_{number}.json 解析失败，已跳过")
            data = empty_glossary()

        key = self._stat_key(path)
        if key is not None:
            self._file_keys[number] = key
        self._cache[number] = data
        return data

    def reset(self) -> None:
        """丢弃所有缓存（外部改动过文件时调用）。

        人工基准（作品级 / 全局）也要**重新读盘**：它们在 __init__ 里一次性载入，
        如果不重读，面板刚写进作品术语库的词条在本次进程内还是看不见，
        紧接着重建 tracker 就会漏掉它（实测踩过）。
        """
        self._cache.clear()
        self._file_keys.clear()
        self._merged = None
        self._merged_upto = -1
        self._estab.clear()

        self.work_glossary = empty_glossary()
        loaded_work = load_glossary_file(self.work_glossary_path)
        if loaded_work is not None:
            self.work_glossary = loaded_work

        self.global_glossary = empty_glossary()
        if self.global_glossary_path:
            loaded_global = load_glossary_file(self.global_glossary_path)
            if loaded_global is not None:
                self.global_glossary = loaded_global

    # ------------------------------------------------- 权威登记（先到先得）

    def _apply(
        self,
        chapter_data: Dict[str, Dict[str, Any]],
        number: int,
        target: Dict,
        estab: Dict,
    ) -> None:
        """把一章的术语并入累计结果。所有类别一律"先到先得"。"""
        for category in ("fixed_terms", "aesthetic_sentences", "cultural_nuances"):
            for key, value in (chapter_data.get(category) or {}).items():
                if not isinstance(value, str) or not value.strip():
                    continue
                if key in target[category]:
                    continue
                target[category][key] = value
                estab[(category, key, None)] = (number, value)

        for key, contexts in (chapter_data.get("contextual_terms") or {}).items():
            if not isinstance(contexts, dict):
                continue
            bucket = target["contextual_terms"].setdefault(key, {})
            if not isinstance(bucket, dict):
                continue
            for ctx_key, ctx_val in contexts.items():
                if not isinstance(ctx_val, str) or not ctx_val.strip():
                    continue
                if ctx_key in bucket:
                    continue  # 同一语境已由更早的章节确立
                bucket[ctx_key] = ctx_val
                estab[("contextual_terms", key, ctx_key)] = (number, ctx_val)

    def merged_before_chapter(self, number: int) -> Dict[str, Dict[str, Any]]:
        """返回"人工基准 + 第 0..number-1 章"的合并结果：**不含第 number 章自己**。

        与 merged_up_to(number) 的区别只在于语义：这里用于"重译/精修"这类
        就地改写的场景 —— 本章 glossary_<n>.json 里的词条往往是上一轮自动
        产出的（模型自己定的译法），把它当成"既定译法"注入会让用户改不动它。
        作品级与全局术语库仍然会注入：那是人工确立的基准。
        """
        return self.merged_up_to(number)

    def merged_up_to(self, target_num: int) -> Dict[str, Dict[str, Any]]:
        """返回"全局术语库 + 第 0..target_num-1 章"的合并结果（先到先得）。

        翻译按章节顺序推进，这里只增量读取新增章节文件，
        整轮任务的文件读取次数是 O(n) 而不是旧版的 O(n²)。
        """
        if self._merged is None or self._merged_upto > target_num:
            self._merged = empty_glossary()
            self._estab = {}
            self._merged_upto = 0
            self._apply(self.work_glossary, AUTHORITY_WORK, self._merged, self._estab)
            self._apply(self.global_glossary, AUTHORITY_GLOBAL, self._merged, self._estab)

        for number in range(self._merged_upto, target_num):
            self._apply(self.chapter(number), number, self._merged, self._estab)
        self._merged_upto = max(self._merged_upto, target_num)

        return self._merged

    def established(
        self, category: str, term: str, context: Optional[str] = None
    ) -> Optional[Tuple[int, str]]:
        """查询某个 (类别, 词, 语境) 的权威译法 → (登记章节, 译法)。"""
        return self._estab.get((category, term, context))

    # ------------------------------------------------------------ 冲突拦截

    def sanitize_chapter(
        self,
        number: int,
        chapter_glos: Dict[str, Dict[str, Any]],
        *,
        allow_changes: bool = False,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """落盘前净化：丢弃与既定译法冲突的重复输出，只保留真正的新词。

        返回 (干净术语库, 冲突列表)。冲突记录里 action 字段：
            ignored    —— 已丢弃，沿用既定译法（默认）
            overridden —— 按 --allow-term-changes 放行，覆盖了既定译法
        """
        clean = empty_glossary()
        conflicts: List[Dict[str, Any]] = []

        def handle(category: str, term: str, context: Optional[str], value: str) -> None:
            value = value.strip()
            if not value:
                return
            authority = self._estab.get((category, term, context))
            if authority is None:
                # 真正的新词：登记
                if category == "contextual_terms":
                    clean[category].setdefault(term, {})[context] = value
                else:
                    clean[category][term] = value
                return

            origin_chapter, old_value = authority
            verdict = compare_translation(old_value, value)
            if verdict == "same":
                # 与既定译法一致：不是新词，无需重复写入
                return

            # 覆盖全局 glossary 是绝对禁止的
            overridable = allow_changes and origin_chapter > AUTHORITY_GLOBAL
            conflicts.append(
                {
                    "category": category,
                    "term": term,
                    "context": context,
                    "old": old_value,
                    "new": value,
                    "origin_chapter": origin_chapter,
                    "origin": {AUTHORITY_WORK: "work", AUTHORITY_GLOBAL: "global"}.get(
                        origin_chapter, "chapter"),
                    "severity": verdict,  # minor=仅大小写/空白差异, different=实质不同
                    "action": "overridden" if overridable else "ignored",
                }
            )
            if overridable:
                if category == "contextual_terms":
                    clean[category].setdefault(term, {})[context] = value
                else:
                    clean[category][term] = value

        for category in ("fixed_terms", "aesthetic_sentences", "cultural_nuances"):
            for term, value in (chapter_glos.get(category) or {}).items():
                if isinstance(value, str):
                    handle(category, term, None, value)

        for term, contexts in (chapter_glos.get("contextual_terms") or {}).items():
            if not isinstance(contexts, dict):
                continue
            for ctx_key, ctx_val in contexts.items():
                if isinstance(ctx_val, str):
                    handle("contextual_terms", term, ctx_key, ctx_val)

        return clean, conflicts

    def record_conflicts(
        self, conflicts: List[Dict[str, Any]], path: Optional[str] = None
    ) -> Optional[str]:
        """把冲突追加写入 glossary_conflicts.jsonl（JSON Lines，便于后续筛选）。"""
        if not conflicts:
            return None
        target = path or os.path.join(self.work_dir, CONFLICTS_FILENAME)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(target, "a", encoding="utf-8") as handle:
                for conflict in conflicts:
                    handle.write(json.dumps({**conflict, "time": stamp}, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.warnings.append(f"冲突记录写入失败: {exc}")
            return None
        return target

    def read_conflicts(self, path: Optional[str] = None) -> List[Dict[str, Any]]:
        """读取历史冲突记录。"""
        target = path or os.path.join(self.work_dir, CONFLICTS_FILENAME)
        if not os.path.exists(target):
            return []
        records: List[Dict[str, Any]] = []
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    # -------------------------------------------------------------- 写入

    def save_chapter(self, number: int, chapter_glos: Dict[str, Dict[str, Any]],
                     *, merge: bool = True) -> str:
        """原子写入单章术语库，并使合并/登记缓存整体失效。

        merge=True（默认）时与文件里**已有条目合并**：同名键用新值覆盖，
        本次没提到的键原样保留。

        为什么必须合并：`merged_up_to(N)` 只读取第 0..N-1 章，**不含第 N 章自己**，
        所以翻译第 N 章时，该章已登记的术语不在 `_estab` 里；模型若不重复提出某个词，
        它就不会出现在净化结果中。若直接整体覆盖，**人工通过面板「指定术语」
        预登记到未来章节的词条会被静默抹掉**（已实测复现）。
        合并后，人工登记与旧译文留下的词条都不会丢；重新翻译只会更新同名词条。
        """
        path = os.path.join(self.work_dir, f"glossary_{number}.json")
        payload = _normalise(chapter_glos)
        if merge:
            payload = _merge_into(self.chapter(number), payload)
        nktext.write_json_atomic(path, payload)

        self._cache[number] = payload
        self._file_keys[number] = self._stat_key(path)
        # 登记表依赖全量顺序，必须整体重建（纯内存 + 每章一次 stat，代价可忽略）
        self._merged = None
        self._merged_upto = -1
        return path

    def existing_chapters(self) -> List[int]:
        """列出目录中已有的 glossary_<n>.json 章节号（升序）。"""
        if not os.path.isdir(self.work_dir):
            return []
        numbers = []
        for name in os.listdir(self.work_dir):
            match = re.match(r"^glossary_(\d+)\.json$", name)
            if match:
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def write_tracker(self, path: Optional[str] = None) -> str:
        """重建并写入 global_glossary_tracker.json。

        与合并规则保持一致：先到先得。旧版对 contextual_terms 会用后续章节的
        语境覆盖前面的，导致 tracker 里 "chapter" 字段（首次出现章节）与
        "value" 字段（被后续章节改过）自相矛盾。
        """
        tracker = {
            "aesthetic_sentences": {},
            "contextual_terms": {},
            "cultural_nuances": {},
            "fixed_terms": {},
        }

        # 人工基准（作品级 > 全局）先入表，章节只能补它们没有的键，
        # 与 merged_up_to 的"先到先得"保持一致。
        def absorb(source: Dict, authority: int) -> None:
            for category in ("fixed_terms", "aesthetic_sentences", "cultural_nuances"):
                for key, value in ((source or {}).get(category) or {}).items():
                    if key not in tracker[category]:
                        tracker[category][key] = {"chapter": authority, "value": value}
            for key, contexts in ((source or {}).get("contextual_terms") or {}).items():
                if not isinstance(contexts, dict):
                    continue
                bucket = tracker["contextual_terms"].setdefault(
                    key, {"chapter": authority, "value": {}})
                if not isinstance(bucket.get("value"), dict):
                    bucket["value"] = {}
                for ctx_key, ctx_val in contexts.items():
                    bucket["value"].setdefault(ctx_key, ctx_val)

        absorb(self.work_glossary, AUTHORITY_WORK)
        absorb(self.global_glossary, AUTHORITY_GLOBAL)

        for number in self.existing_chapters():
            chapter_data = self.chapter(number)

            for category in ("fixed_terms", "aesthetic_sentences", "cultural_nuances"):
                for key, value in (chapter_data.get(category) or {}).items():
                    if key not in tracker[category]:
                        tracker[category][key] = {"chapter": number, "value": value}

            for key, contexts in (chapter_data.get("contextual_terms") or {}).items():
                if not isinstance(contexts, dict):
                    continue
                bucket = tracker["contextual_terms"].setdefault(
                    key, {"chapter": number, "value": {}}
                )
                if not isinstance(bucket.get("value"), dict):
                    bucket["value"] = {}
                for ctx_key, ctx_val in contexts.items():
                    bucket["value"].setdefault(ctx_key, ctx_val)

        target = path or os.path.join(self.work_dir, "global_glossary_tracker.json")
        nktext.write_json_atomic(target, tracker, sort_keys=True)
        return target

    # -------------------------------------------------------------- 过滤

    def _matcher(self, key: str) -> Optional[re.Pattern]:
        """非中文词条的整词匹配器（中文词条用子串匹配即可）。"""
        if key in self._matcher_cache:
            return self._matcher_cache[key]
        if nktext.CJK_RE.search(key) or len(key) < 2:
            pattern: Optional[re.Pattern] = None
        else:
            pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(key)}(?![0-9A-Za-z])", re.IGNORECASE)
        self._matcher_cache[key] = pattern
        return pattern

    def key_present(self, key: str, scope: str, scope_lower: str) -> bool:
        """判断术语是否出现在待译内容（含后文）中。

        旧版一律用 `key in scope` 子串匹配，像 "Li"、"An" 这样的短英文键
        会命中 "Liar"、"Ancient"，把无关术语塞进提示词里污染翻译。
        这里对非中文键改用整词边界匹配。
        """
        if not key:
            return False
        if nktext.CJK_RE.search(key):
            return key in scope

        lowered = key.lower()
        if lowered not in scope_lower:
            return False
        pattern = self._matcher(key)
        if pattern is None:
            return True
        return pattern.search(scope) is not None

    def filter_for_content(
        self,
        glossary: Dict[str, Dict[str, Any]],
        content: str,
        future_content: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """只保留在当前章节（含后文参考）中真正出现的术语。"""
        scope = content + (future_content or "")
        scope_lower = scope.lower()
        filtered = empty_glossary()

        for category in CATEGORIES:
            for key, value in (glossary or {}).get(category, {}).items():
                if self.key_present(str(key), scope, scope_lower):
                    filtered[category][key] = value
        return filtered
