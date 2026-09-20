"""术语库人工维护：面板里新增/修正一条术语。

三类作用范围（优先级从高到低）：
    work    写入 <作品>/glossary.json —— 这部作品里最高
    global  写入 config/glossary.json —— 所有作品共用（自动流程永不覆盖）
    chapter 写入 glossary_<章号>.json —— 默认，先到先得

术语分四类，值形态不同：
    fixed_terms / aesthetic_sentences / cultural_nuances  扁平 term → 译法或说明
    contextual_terms                                      term → 语境 → 译法
只有固定术语与语境术语要求"键是中文、值是纯英文"；后两类的值是说明文字。

必须守住"先到先得"这条不变量
--------------------------
术语的权威值记在**最早确立它的那一章**的 glossary 文件里，读取时 first-wins。
所以"修改某条术语"不能往当前章再写一遍（那样不生效），而要回写到它的来源章。
本模块按这个规则处理：

  * 术语尚未确立 → 写进指定章（默认取已有 glossary 的最大章号）
  * 已确立且值相同 → 什么都不做，报告"已存在"
  * 已确立但值不同 → 默认**拒绝**并回显旧值，需显式 force 才回写来源章
  * 来源于全局 glossary.json → 章节层不能改它（人工基准最高）：把作用范围选成
    「全局术语库」即可改（勾选强制覆盖），或直接编辑 config/glossary.json
  * 作用范围 = "global" → 写 config/glossary.json（所有作品共用）

一切写入后都会重建 global_glossary_tracker.json，保持派生数据自洽。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from novelkit import glossary as nkglossary  # noqa: E402
from novelkit import text as nktext  # noqa: E402

# 与 novelkit.glossary.CATEGORIES 一致
CATEGORIES = ("fixed_terms", "contextual_terms", "aesthetic_sentences", "cultural_nuances")

# 只有语境术语是"嵌套 dict"（term → context → 译法），其余三类都是扁平 term → 说明
NESTED_CATEGORIES = ("contextual_terms",)
# 这三类的"译法"是说明性文字，允许含中文；固定/语境术语必须纯英文
EXPLANATORY_CATEGORIES = ("aesthetic_sentences", "cultural_nuances")
# 这三类的键必须是中文词条；文化/美学类的键是个说法，可以是任何短串
CJK_KEY_CATEGORIES = ("fixed_terms", "contextual_terms")

CATEGORY_LABELS = {
    "fixed_terms": "固定术语",
    "contextual_terms": "语境术语",
    "aesthetic_sentences": "美学表达",
    "cultural_nuances": "文化虚指",
}


def _lookup_established(store: nkglossary.GlossaryStore, term: str,
                        context: Optional[str]) -> Optional[Dict[str, Any]]:
    """在 fixed_terms / contextual_terms 里找这条术语的权威记录。"""
    for category in CATEGORIES:
        found = store.established(category, term, context)
        if found is None and category in NESTED_CATEGORIES:
            # 语境键未知时，看看该词条有没有任何语境记录
            for key, value in store._estab.items():  # noqa: SLF001 - 需要遍历私有登记表
                if key[0] == "contextual_terms" and key[1] == term:
                    return {"category": category, "chapter": value[0],
                            "value": value[1], "context": key[2]}
            continue
        if found is not None:
            chapter, value = found
            return {"category": category, "chapter": chapter, "value": value,
                    "context": context}
    return None


def _write_entry(data: Dict[str, Dict[str, Any]], term: str, category: str,
                 context: str, translation: str) -> None:
    """把一条术语写进 glossary 数据结构（语境类是嵌套 dict，其余三类是扁平字符串）。"""
    if category in NESTED_CATEGORIES:
        data[category].setdefault(term, {})[context] = translation
    else:
        data[category][term] = translation


def read_global(global_glossary_path: Optional[str], label: str = "") -> Dict[str, Any]:
    """读取全局术语库，按类别整理成面板可直接渲染的结构。"""
    if not global_glossary_path:
        return {"ok": False, "error": "面板没有配置全局术语库路径"}
    raw = nkglossary.load_glossary_file(str(global_glossary_path)) or {}
    entries: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for category in CATEGORIES:
        items = raw.get(category) or {}
        counts[category] = len(items)
        for term, value in items.items():
            entries.append({"term": term, "category": category, "value": value,
                            "scope": "global", "chapter": -1})
    return {
        "ok": True,
        "scope": "global",
        "label": label or "全局术语库",
        "entries": entries,
        "counts": counts,
        "total": sum(counts.values()),
        "categories": [{"id": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES],
        "path": global_glossary_path,
    }


def add_term(work_dir: Path, global_glossary_path: Optional[str], *,
             term: str, translation: str, category: str = "fixed_terms",
             context: str = "", chapter: Optional[int] = None,
             force: bool = False, scope: str = "chapter") -> Dict[str, Any]:
    """新增/修正一条术语。

    scope:
      "global"  → 写入 config/glossary.json，**所有作品共用**（跨作品基准）。
                  这是唯一由人手工维护、自动流程永不覆盖的一层。
      "work"    → 写入 <作品>/glossary.json，**整本书最高优先级**。
                  适合原著术语、关键设定、人名等"这本书专用"的基准。
      "chapter" → 写入 glossary_<chapter>.json（默认），遵守先到先得。
    """
    term = (term or "").strip()
    translation = (translation or "").strip()
    category = category if category in CATEGORIES else "fixed_terms"
    context = (context or "").strip()

    label = CATEGORY_LABELS.get(category, category)

    if not term:
        return {"ok": False, "error": "请填写词条"}
    if not translation:
        return {"ok": False, "error": f"请填写{label}的内容"}
    if category in CJK_KEY_CATEGORIES and not nktext.has_cjk(term):
        return {"ok": False, "error": "术语的词条应是中文（术语库的键是中文原词）"}
    if category not in EXPLANATORY_CATEGORIES and nktext.has_cjk(translation):
        return {"ok": False, "error": "译法必须是纯英文（不能含中文）"}
    if category == "contextual_terms" and not context:
        return {"ok": False, "error": "语境术语必须填写语境说明"}
    # 固定术语的键就是译名，没有"语境"这回事：填了就明确拒绝，
    # 而不是悄悄丢掉（这是以前的 bug）。
    if category in ("fixed_terms",) and context:
        return {"ok": False,
                "error": "「固定术语」不接受语境说明：它是全篇统一的译名。"
                         "要按语境区分译法，请把类别改成「语境术语」。"}

    if scope not in ("global", "work", "chapter"):
        return {"ok": False, "error": f"未知的作用范围: {scope}"}

    store = nkglossary.GlossaryStore(str(work_dir), global_glossary_path)

    # ---- 全局级：写 config/glossary.json（所有作品共用）----
    # 与其它层不同，这里**允许**人工写：自动流程不许覆盖它，
    # 但"用户自己在面板里编辑"正是它被维护的方式。
    if scope == "global":
        if not global_glossary_path:
            return {"ok": False, "error": "面板没有配置全局术语库路径"}
        current = None
        for cat in CATEGORIES:
            value = (store.global_glossary.get(cat) or {}).get(term)
            if value is not None:
                current = (cat, value)
                break
        if current is not None:
            cat, value = current
            shown = value if isinstance(value, str) else " / ".join(str(v) for v in value.values())
            if str(shown).strip() == translation and cat == category:
                return {"ok": True, "action": "unchanged",
                        "message": f"全局术语库已有该词条且一致：{term} → {shown}"}
            if not force:
                return {"ok": False, "conflict": True,
                        "error": f"全局术语库已有 '{term}' = “{shown}”；"
                                 f"确要改成 “{translation}” 请勾选「强制覆盖」",
                        "old_value": shown, "source": "global"}

        data = nkglossary.load_glossary_file(str(global_glossary_path)) or nkglossary.empty_glossary()
        _write_entry(data, term, category, context, translation)
        # 同一个词条只留在选定的那一类里，避免四类里各有一份、互相矛盾
        for other in CATEGORIES:
            if other != category:
                data[other].pop(term, None)

        path = Path(global_glossary_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        nktext.write_json_atomic(str(path), data)

        # 全局层进了 tracker 的 chapter = -1（表示"不属于任何章"）
        store.reset()
        store.write_tracker()
        return {"ok": True, "action": "added" if current is None else "overridden",
                "message": f"已写入全局术语库（所有作品共用）：{term} → {translation}",
                "scope": "global", "file": path.name, "global_path": str(path)}

    # ---- 作品级：直接写 <作品>/glossary.json ----
    if scope == "work":
        value = (store.work_glossary.get(category) or {}).get(term)
        if category in NESTED_CATEGORIES:
            value = (value or {}).get(context) if isinstance(value, dict) else None
        if value is not None and str(value).strip() == translation:
            return {"ok": True, "action": "unchanged",
                    "message": f"作品术语库已有该词条且一致：{term} → {translation}"}
        if value is not None and not force:
            return {"ok": False, "conflict": True,
                    "error": f"作品术语库已有 '{term}' = “{value}”；"
                             f"确要改成 “{translation}” 请勾选「强制覆盖」",
                    "old_value": value}
        data = nkglossary.load_glossary_file(str(store.work_glossary_path)) or nkglossary.empty_glossary()
        _write_entry(data, term, category, context, translation)
        work_dir.mkdir(parents=True, exist_ok=True)
        nktext.write_json_atomic(str(store.work_glossary_path), data)
        store.reset()
        store.write_tracker()
        return {"ok": True, "action": "added" if value is None else "overridden",
                "message": f"已写入作品术语库（整本书最高优先级）：{term} → {translation}",
                "scope": "work", "file": nkglossary.WORK_GLOSSARY_FILENAME}

    # 先把所有章节读进来，才能知道这条术语最早出现在哪一章
    existing = store.existing_chapters()
    target_num = chapter if chapter is not None else (max(existing) if existing else 1)
    store.merged_up_to(max(target_num, (max(existing) + 1) if existing else 1))

    # 作品级 / 全局 glossary.json 都是人工基准，章节不能覆盖它们；
    # 要改请把作用范围选成「整本书」。
    work_hit = None
    for cat in CATEGORIES:
        value = (store.work_glossary.get(cat) or {}).get(term)
        if value is not None:
            work_hit = {"category": cat, "value": value}
            break
    if work_hit is not None:
        shown = work_hit["value"]
        if isinstance(shown, dict):
            shown = " / ".join(str(v) for v in shown.values())
        if str(shown).strip() == translation and work_hit["category"] == category:
            return {"ok": True, "action": "unchanged",
                    "message": f"作品术语库已有该词条且一致：{term} → {shown}"}
        return {"ok": False,
                "error": f"'{term}' 已在作品术语库中定为 {shown}（整本书最高优先级）。"
                         "要改它，请把「作用范围」选成「整本书」。",
                "source": "work"}

    # 全局 glossary.json 是人工基准，面板不越权修改
    global_hit = None
    for cat in CATEGORIES:
        value = (store.global_glossary.get(cat) or {}).get(term)
        if value is not None:
            global_hit = {"category": cat, "value": value}
            break
    if global_hit is not None:
        shown = global_hit["value"]
        if isinstance(shown, dict):
            shown = " / ".join(str(v) for v in shown.values())
        if str(shown).strip() == translation and global_hit["category"] == category:
            return {"ok": True, "action": "unchanged",
                    "message": f"全局术语库已有该词条且一致：{term} → {shown}"}
        return {"ok": False,
                "error": f"'{term}' 来自全局术语库（当前为 {shown}），"
                         "全局术语库是最高基准，请在 config/glossary.json 里修改",
                "source": "global"}

    established = _lookup_established(store, term, context or None)

    # ---- 已存在的情况 ----
    if established is not None:
        origin_chapter = established["chapter"]
        old_value = established["value"]
        same = (str(old_value).strip() == translation)
        same_category = established["category"] == category
        if same and same_category and (not context or established.get("context") == context):
            return {"ok": True, "action": "unchanged",
                    "message": f"术语 '{term}' 已存在且一致（第 {origin_chapter} 章）"}
        if not force:
            return {
                "ok": False,
                "conflict": True,
                "error": f"'{term}' 已在第 {origin_chapter} 章确立为 “{old_value}”；"
                         f"确要改成 “{translation}” 请勾选「强制覆盖」",
                "origin_chapter": origin_chapter,
                "old_value": old_value,
            }
        if origin_chapter < 0:
            return {"ok": False, "error": "该词条来自全局术语库，面板不覆盖"}
        write_chapter = origin_chapter
        action = "overridden"
        note = f"已覆盖第 {origin_chapter} 章的既定译法"
    else:
        write_chapter = target_num
        action = "added"
        note = f"已写入第 {write_chapter} 章术语库"

    # ---- 落盘 ----
    path = work_dir / f"glossary_{write_chapter}.json"
    data = nkglossary.load_glossary_file(str(path)) or nkglossary.empty_glossary()
    _write_entry(data, term, category, context, translation)
    work_dir.mkdir(parents=True, exist_ok=True)
    nktext.write_json_atomic(str(path), data)

    store.reset()
    tracker = store.write_tracker()

    return {
        "ok": True,
        "action": action,
        "message": f"{note}：{term} → {translation}",
        "chapter": write_chapter,
        "category": category,
        "context": context or None,
        "file": path.name,
        "tracker": Path(tracker).name,
    }


def delete_term(work_dir: Path, global_glossary_path: Optional[str], *,
                term: str, context: str = "") -> Dict[str, Any]:
    """删除一条术语（从它来源章的 glossary 文件里移除）。"""
    term = (term or "").strip()
    if not term:
        return {"ok": False, "error": "请指定词条"}
    store = nkglossary.GlossaryStore(str(work_dir), global_glossary_path)
    for cat in CATEGORIES:
        if (store.work_glossary.get(cat) or {}).get(term) is not None:
            return {"ok": False,
                    "error": f"'{term}' 来自作品术语库（{nkglossary.WORK_GLOSSARY_FILENAME}），"
                             "请在那里删除，或改作用范围为「整本书」"}
        if (store.global_glossary.get(cat) or {}).get(term) is not None:
            return {"ok": False, "error": f"'{term}' 来自全局 glossary.json，请在那里删除"}
    existing = store.existing_chapters()
    if existing:
        store.merged_up_to(max(existing) + 1)
    found = _lookup_established(store, term, context or None)
    if found is None:
        return {"ok": False, "error": f"术语库里没有 '{term}'"}
    chapter = found["chapter"]
    path = work_dir / f"glossary_{chapter}.json"
    data = nkglossary.load_glossary_file(str(path)) or nkglossary.empty_glossary()
    if found["category"] == "contextual_terms":
        bucket = data["contextual_terms"].get(term)
        if isinstance(bucket, dict):
            bucket.pop(found.get("context"), None)
            if not bucket:
                data["contextual_terms"].pop(term, None)
    else:
        data[found["category"]].pop(term, None)
    nktext.write_json_atomic(str(path), data)
    store.reset()
    store.write_tracker()
    return {"ok": True, "action": "deleted",
            "message": f"已从第 {chapter} 章术语库删除 '{term}'", "chapter": chapter}


def search(work_dir: Path, global_glossary_path: Optional[str], term: str) -> Dict[str, Any]:
    """查询一条术语的权威记录（供模态框在提交前提示冲突）。"""
    term = (term or "").strip()
    if not term:
        return {"ok": True, "found": False}
    store = nkglossary.GlossaryStore(str(work_dir), global_glossary_path)
    existing = store.existing_chapters()
    if existing:
        store.merged_up_to(max(existing) + 1)
    found = _lookup_established(store, term, None)
    for cat in CATEGORIES:
        value = (store.work_glossary.get(cat) or {}).get(term)
        if value is not None:
            shown = value if isinstance(value, str) else " / ".join(str(v) for v in value.values())
            return {"ok": True, "found": True, "source": "work",
                    "category": cat, "value": shown, "scope": "work"}
    for cat in CATEGORIES:
        value = (store.global_glossary.get(cat) or {}).get(term)
        if value is not None:
            shown = value if isinstance(value, str) else " / ".join(str(v) for v in value.values())
            return {"ok": True, "found": True, "source": "global",
                    "category": cat, "value": shown}
    if found is None:
        return {"ok": True, "found": False}
    return {"ok": True, "found": True, "source": "chapter",
            "category": found["category"], "chapter": found["chapter"],
            "value": found["value"], "context": found.get("context")}
