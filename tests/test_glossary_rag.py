#!/usr/bin/env python3
"""术语库一致性规则 + RAG 检索的离线测试。

对应本次两项需求：
  A. 术语库合并/覆盖规则收紧：全局优先、先到先得、落盘前拦截重复输出，
     确保新译文不会违背已发布的旧正文。
  B. RAG 检索：挑出"可能有特定内涵"的词，回到全书（默认后文）检索用法语境。

运行： python3 tests/test_glossary_rag.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from novelkit import glossary as nkglossary  # noqa: E402
from novelkit import rag as nkrag  # noqa: E402
from novelkit import text as nktext  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, func) -> None:
    try:
        func()
    except Exception:  # noqa: BLE001
        RESULTS.append((name, False, traceback.format_exc(limit=3)))
    else:
        RESULTS.append((name, True, ""))


def expect(condition, message: str = "") -> None:
    if not condition:
        raise AssertionError(message or "断言失败")


def empty() -> dict:
    return nkglossary.empty_glossary()


def make_store(tmp: str, global_glos: dict | None = None) -> nkglossary.GlossaryStore:
    path = None
    if global_glos is not None:
        path = os.path.join(tmp, "_global_glossary.json")
        nktext.write_json_atomic(path, global_glos)
    return nkglossary.GlossaryStore(tmp, path)


# ==========================================================================
# A. 术语库规则
# ==========================================================================

def test_global_wins_over_chapter() -> None:
    """全局 glossary.json 是人工基准，章节不得覆盖。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp, {"fixed_terms": {"主城": "Major Hub"}})
        store.merged_up_to(1)

        clean, conflicts = store.sanitize_chapter(1, {"fixed_terms": {"主城": "Main City"}})
        expect(clean["fixed_terms"] == {}, f"冲突条目不应写入: {clean['fixed_terms']}")
        expect(len(conflicts) == 1, str(conflicts))
        expect(conflicts[0]["origin"] == "global", str(conflicts[0]))
        expect(conflicts[0]["action"] == "ignored", str(conflicts[0]))

        merged = store.merged_up_to(2)
        expect(merged["fixed_terms"]["主城"] == "Major Hub", str(merged["fixed_terms"]))
    finally:
        shutil.rmtree(tmp)


def test_merge_glossaries_global_wins() -> None:
    """merge_glossaries：全局优先，同一语境先到先得。"""
    global_glos = {"fixed_terms": {"a": "A"}, "contextual_terms": {"c": {"x": "1"}}}
    local = {"fixed_terms": {"a": "Z", "b": "B"}, "contextual_terms": {"c": {"x": "9", "y": "2"}}}
    merged = nkglossary.merge_glossaries(global_glos, local)
    expect(merged["fixed_terms"] == {"a": "A", "b": "B"}, str(merged["fixed_terms"]))
    expect(merged["contextual_terms"]["c"] == {"x": "1", "y": "2"}, str(merged["contextual_terms"]))


def test_context_first_wins_per_key() -> None:
    """同一 (词, 语境) 只认第一次；新增语境仍然接受。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_0.json"),
            {"contextual_terms": {"暴毙": {"英文": "instantly die"}}},
        )
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_5.json"),
            {"contextual_terms": {"暴毙": {"英文": "die suddenly"}}},
        )
        merged = store.merged_up_to(6)
        expect(
            merged["contextual_terms"]["暴毙"]["英文"] == "instantly die",
            f"第 5 章不应改写第 0 章已定的语境译法: {merged['contextual_terms']['暴毙']}",
        )

        # 新增一个此前没有的语境 → 应当被接受
        store.merged_up_to(6)
        clean, conflicts = store.sanitize_chapter(6, {"contextual_terms": {"暴毙": {"口语": "kick the bucket"}}})
        expect(clean["contextual_terms"]["暴毙"]["口语"] == "kick the bucket", str(clean))
        expect(conflicts == [], str(conflicts))
    finally:
        shutil.rmtree(tmp)


def test_conflicting_reexpression_blocked_and_recorded() -> None:
    """模型重复输出已定词条且译法不同 → 丢弃 + 记录冲突。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.save_chapter(0, {"fixed_terms": {"主城": "Major Hub"}})
        store.merged_up_to(1)

        clean, conflicts = store.sanitize_chapter(
            5, {"fixed_terms": {"主城": "Main City", "新地名": "New Place"}}
        )
        expect(clean["fixed_terms"] == {"新地名": "New Place"}, str(clean["fixed_terms"]))
        expect(len(conflicts) == 1 and conflicts[0]["term"] == "主城", str(conflicts))

        path = store.record_conflicts(conflicts)
        expect(path and os.path.exists(path), "应写出冲突记录文件")
        records = store.read_conflicts()
        expect(len(records) == 1, str(records))
        expect(records[0]["old"] == "Major Hub" and records[0]["new"] == "Main City", str(records[0]))
        expect(records[0]["origin_chapter"] == 0, str(records[0]))
    finally:
        shutil.rmtree(tmp)


def test_identical_reexpression_is_silent() -> None:
    """与既定译法一致时既不报冲突，也不重复写入。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.save_chapter(0, {"fixed_terms": {"主城": "Major Hub"}})
        store.merged_up_to(1)
        clean, conflicts = store.sanitize_chapter(5, {"fixed_terms": {"主城": "Major Hub"}})
        expect(clean["fixed_terms"] == {}, str(clean))
        expect(conflicts == [], str(conflicts))
    finally:
        shutil.rmtree(tmp)


def test_case_only_difference_is_minor() -> None:
    """仅大小写/空白差异标记为 minor，便于人工忽略。"""
    expect(nkglossary.compare_translation("EXP Card", "EXP card") == "minor")
    expect(nkglossary.compare_translation("a  b", "a b") == "minor")
    expect(nkglossary.compare_translation("Major Hub", "Main City") == "different")
    expect(nkglossary.compare_translation("Same", "Same") == "same")

    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.save_chapter(0, {"contextual_terms": {"经验卡": {"戏称": "EXP Card"}}})
        store.merged_up_to(1)
        _, conflicts = store.sanitize_chapter(5, {"contextual_terms": {"经验卡": {"戏称": "EXP card"}}})
        expect(len(conflicts) == 1 and conflicts[0]["severity"] == "minor", str(conflicts))
    finally:
        shutil.rmtree(tmp)


def test_retranslating_earlier_chapter_cannot_rewrite_history() -> None:
    """重译更早的章节（--force）不得改变已被后续章节使用的既定译法。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.save_chapter(3, {"fixed_terms": {"云舟": "Memory Retainer"}})
        store.save_chapter(10, {"fixed_terms": {"石昊": "Shi Hao"}})

        store.merged_up_to(11)
        clean, conflicts = store.sanitize_chapter(3, {"fixed_terms": {"云舟": "Memory Servant"}})
        expect(clean["fixed_terms"] == {}, f"默认必须拦截: {clean}")
        expect(conflicts[0]["action"] == "ignored", str(conflicts))

        # 全局 glossary 即便显式 --allow-term-changes 也不能被覆盖
        store2 = make_store(tmp, {"fixed_terms": {"天": "Heaven"}})
        store2.merged_up_to(1)
        clean2, conflicts2 = store2.sanitize_chapter(
            1, {"fixed_terms": {"天": "Sky"}}, allow_changes=True
        )
        expect(clean2["fixed_terms"] == {}, "全局术语永不可覆盖")
        expect(conflicts2[0]["action"] == "ignored", str(conflicts2))
    finally:
        shutil.rmtree(tmp)


def test_allow_term_changes_overrides_chapter_terms() -> None:
    """--allow-term-changes 可以覆盖章节既定译法，但会标记为 overridden。"""
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.save_chapter(3, {"fixed_terms": {"云舟": "Memory Retainer"}})
        store.merged_up_to(11)

        clean, conflicts = store.sanitize_chapter(
            10, {"fixed_terms": {"云舟": "Memory Servant"}}, allow_changes=True
        )
        expect(clean["fixed_terms"]["云舟"] == "Memory Servant", str(clean))
        expect(conflicts[0]["action"] == "overridden", str(conflicts))
    finally:
        shutil.rmtree(tmp)


def test_tracker_chapter_and_value_consistent() -> None:
    """tracker 的 chapter 字段必须与 value 来源一致（旧版会自相矛盾）。"""
    tmp = tempfile.mkdtemp()
    try:
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_0.json"),
            {"contextual_terms": {"C": {"x": "first"}}},
        )
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_5.json"),
            {"contextual_terms": {"C": {"x": "later", "y": "new"}}},
        )
        store = make_store(tmp)
        tracker = json.loads(Path(store.write_tracker()).read_text(encoding="utf-8"))
        entry = tracker["contextual_terms"]["C"]
        expect(entry["chapter"] == 0, str(entry))
        expect(entry["value"] == {"x": "first", "y": "new"}, str(entry))
    finally:
        shutil.rmtree(tmp)


def test_sanitize_keeps_new_terms_and_drops_non_strings() -> None:
    tmp = tempfile.mkdtemp()
    try:
        store = make_store(tmp)
        store.merged_up_to(1)
        clean, conflicts = store.sanitize_chapter(
            1,
            {
                "fixed_terms": {"好人": "good person", "垃圾": 123, "空的": "  "},
                "contextual_terms": {"词": {"语境": "ctx"}, "坏的": "不是字典"},
            },
        )
        expect(clean["fixed_terms"] == {"好人": "good person"}, str(clean["fixed_terms"]))
        expect(clean["contextual_terms"] == {"词": {"语境": "ctx"}}, str(clean["contextual_terms"]))
        expect(conflicts == [])
    finally:
        shutil.rmtree(tmp)


# ==========================================================================
# B. RAG 检索
# ==========================================================================

CORPUS = {
    0: "这是一部测试小说的简介，主角叫林越。",
    1: (
        "林越走进苍岚之森，看见一棵巨大的苍穹神木，"
        "苍穹神木散发着微光。云舟站在树下，云舟抬头望着苍穹神木，云舟沉默不语。"
    ),
    2: "云舟说道：神树的力量来源于记忆。林越点了点头。",
    3: "多年以后，林越再次来到苍穹神木前，云舟已经离开，只留下石昊。",
    4: "石昊告诉林越，云舟当年把记忆留在了苍穹神木里。",
}


def make_corpus() -> str:
    tmp = tempfile.mkdtemp(prefix="novelkit_rag_")
    for number, content in CORPUS.items():
        Path(tmp, f"{number}_origin.txt").write_text(content, encoding="utf-8")
    return tmp


def test_corpus_index_stats() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        expect(index.chapter_count() == 5, str(index.chapter_count()))
        chapters, total = index.stats("云舟")
        expect(total == 6, f"云舟 总出现次数 {total}")
        expect(chapters == 4, f"云舟 出现章节数 {chapters}")
        expect(index.chapters_containing("石昊") == [3, 4], str(index.chapters_containing("石昊")))
    finally:
        shutil.rmtree(tmp)


def test_search_future_only_and_excludes_current() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        snippets = index.search("云舟", current=1, limit=2, window=60, scope="future")
        expect(len(snippets) == 2, f"应取到 2 条: {snippets}")
        expect(all(s.chapter > 1 for s in snippets), f"不应包含当前章节或前文: {snippets}")
        expect(all("云舟" in s.text for s in snippets), f"片段必须包含检索词: {snippets}")
        expect(all(s.side == "future" for s in snippets), str(snippets))

        past = index.search("云舟", current=2, limit=1, window=60, scope="past")
        expect(len(past) == 1 and past[0].chapter == 1, str(past))
        expect(past[0].side == "past", str(past))
    finally:
        shutil.rmtree(tmp)


def test_search_all_prefers_future() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        snippets = index.search("苍穹神木", current=2, limit=2, window=60, scope="all")
        expect(len(snippets) == 2, str(snippets))
        expect(snippets[0].side == "future", f"all 应先给后文: {snippets}")
    finally:
        shutil.rmtree(tmp)


def test_extract_candidates_suppresses_fragments() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        chapter_text = CORPUS[1]
        candidates = nkrag.extract_candidates(chapter_text, empty(), index, max_terms=6)
        terms = [c.term for c in candidates]
        expect(terms, "应当发现候选词")
        expect("云舟" in terms, f"云舟 应被选出: {terms}")
        expect("苍穹神木" in terms, f"苍穹神木 应被选出: {terms}")
        # 子串碎片不应与完整词条同时出现
        for a in terms:
            for b in terms:
                if a != b:
                    expect(a not in b, f"候选词互为子串: {a} ⊂ {b} ({terms})")
    finally:
        shutil.rmtree(tmp)


def test_extract_candidates_priorities() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        chapter_text = CORPUS[1]

        # fixed_terms 已有定译 → 不再作为候选
        fixed = {"fixed_terms": {"云舟": "Memory Retainer"}}
        terms = [c.term for c in nkrag.extract_candidates(chapter_text, fixed, index, max_terms=8)]
        expect("云舟" not in terms, f"已定译法的词不该再检索: {terms}")

        # contextual_terms → 最高优先级，排在最前
        contextual = {"contextual_terms": {"苍穹神木": {"比喻": "Tree"}}}
        cands = nkrag.extract_candidates(chapter_text, contextual, index, max_terms=8)
        expect(cands[0].term == "苍穹神木" and cands[0].source == "contextual", str(cands[0]))

        # cultural_nuances → 优先级更高
        cultural = {"cultural_nuances": {"云舟": "特殊称谓，建议意译。"}}
        cands2 = nkrag.extract_candidates(chapter_text, cultural, index, max_terms=8)
        expect(cands2[0].term == "云舟" and cands2[0].source == "cultural", str(cands2[0]))
    finally:
        shutil.rmtree(tmp)


def test_common_terms_are_penalised_using_true_chapter_count() -> None:
    """全书到处都是的词应被淘汰；打分必须用真实章节数而非被截断的样本长度。"""
    tmp = tempfile.mkdtemp(prefix="novelkit_rag_common_")
    try:
        # 20 章语料：常见词 出现在 18 章（每章语境都不同），稀有词 只出现在 2 章
        for number in range(20):
            if number < 18:
                body = (
                    f"第{number}天，他在路口看见了常见词，心里纳闷。"
                    f"那枚常见词静静躺在第{number}个架子上。"
                )
            else:
                body = f"第{number}天，他什么都没看见。"
            if number in (1, 2):
                body += "这时他第一次注意到稀有词，稀有词泛着微光。"
            Path(tmp, f"{number}_origin.txt").write_text(body, encoding="utf-8")

        index = nkrag.CorpusIndex(tmp)
        chapter_text = Path(tmp, "1_origin.txt").read_text(encoding="utf-8")
        candidates = nkrag.extract_candidates(chapter_text, empty(), index, max_terms=5)
        terms = [c.term for c in candidates]

        expect("稀有词" in terms, f"稀有词应入选: {terms}")
        expect("常见词" not in terms, f"遍及全书的常见词应被淘汰: {terms}")

        # 统计口径：章节数必须是真实值，样本列表才受上限约束
        hits, total, sample = index.occurrence_stats("常见词")
        expect(hits == 18, f"真实章节数应为 18，实际 {hits}")
        expect(len(sample) <= 24, "样本列表应受上限约束")
        expect(total > hits, f"总次数应大于章节数: {total}")
    finally:
        shutil.rmtree(tmp)


def test_researcher_and_render() -> None:
    tmp = make_corpus()
    try:
        researcher = nkrag.TermResearcher(tmp, scope="future", window=60, snippets_per_term=2, max_terms=4)
        glossary = {
            "contextual_terms": {
                "苍穹神木": {"比喻": "Prismatic Divine Tree"},
                "云舟": {"尊称": "Memory Retainer"},
            },
            "cultural_nuances": {"林越": "人名，音译为 Lin Yue。"},
        }
        findings = researcher.research(CORPUS[1], glossary, current_chapter=1)
        expect(findings, "应当有检索结果")
        by_term = {f.term: f for f in findings}

        expect("苍穹神木" in by_term, str(list(by_term)))
        expect(by_term["苍穹神木"].existing.get("比喻") == "Prismatic Divine Tree")
        expect(by_term["苍穹神木"].snippets, "苍穹神木 应有后文片段")

        expect("林越" in by_term, f"文化词条应入选: {list(by_term)}")
        expect(by_term["林越"].note and "Lin Yue" in by_term["林越"].note, str(by_term["林越"]))

        block = researcher.render(findings)
        expect(block.startswith("<term_context_research>"), block[:80])
        expect(block.rstrip().endswith("</term_context_research>"), block[-80:])
        expect('word="苍穹神木"' in block, block[:400])
        expect("established_context" in block, "应带既定语境译法")
        expect("localization_note" in block, "文化词条应带 localization_note")
        expect("<future chapter=" in block, "应有后文片段")
        expect("严禁" in block, "必须带禁止剧透的说明")

        # 已有 fixed 译法的词不参与检索（译法已定，没有选择余地）
        fixed_glossary = {"fixed_terms": {"云舟": "Memory Retainer"}}
        fixed_terms = [
            c.term for c in nkrag.extract_candidates(CORPUS[1], fixed_glossary, nkrag.CorpusIndex(tmp), max_terms=8)
        ]
        expect("云舟" not in fixed_terms, f"fixed 词不应参与检索: {fixed_terms}")
    finally:
        shutil.rmtree(tmp)


def test_render_budget_truncates() -> None:
    tmp = make_corpus()
    try:
        researcher = nkrag.TermResearcher(tmp, scope="all", window=60, snippets_per_term=3,
                                          max_terms=10, budget=500)
        glossary = {"contextual_terms": {t: {"x": "y"} for t in ("云舟", "苍穹神木", "林越", "石昊")}}
        findings = researcher.research(CORPUS[1], glossary, current_chapter=1)
        block = researcher.render(findings)
        expect(len(block) <= 1600, f"预算应生效，实际 {len(block)}")
    finally:
        shutil.rmtree(tmp)


def test_render_empty_when_no_findings() -> None:
    tmp = make_corpus()
    try:
        researcher = nkrag.TermResearcher(tmp)
        expect(researcher.render([]) == "", "无结果时不应注入任何内容")
    finally:
        shutil.rmtree(tmp)


def test_search_last_chapter_has_no_future() -> None:
    tmp = make_corpus()
    try:
        index = nkrag.CorpusIndex(tmp)
        expect(index.search("云舟", current=4, limit=2, scope="future") == [])
    finally:
        shutil.rmtree(tmp)


# ==========================================================================
# C. 端到端：翻译流程中同时生效
# ==========================================================================

class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0) if self.replies else "{}"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=FakeMessage(content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class FakeClient:
    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=FakeCompletions(replies))

    @property
    def calls(self):
        return self.chat.completions.calls


def build_workspace() -> str:
    tmp = tempfile.mkdtemp(prefix="novelkit_e2e2_")
    Path(tmp, "0_origin.txt").write_text(CORPUS[0], encoding="utf-8")
    Path(tmp, "0_translated.txt").write_text("Introduction.", encoding="utf-8")
    # 第 1 章正文里必须真的出现 主城，否则会被"术语相关性过滤"剔除（这是预期行为）
    Path(tmp, "1_origin.txt").write_text(
        CORPUS[1] + "他望向主城的方向，主城依旧灯火通明。", encoding="utf-8"
    )
    for number in (2, 3):
        Path(tmp, f"{number}_origin.txt").write_text(CORPUS[number], encoding="utf-8")
    # 第 0 章已经确立 主城 = Major Hub
    nktext.write_json_atomic(
        os.path.join(tmp, "glossary_0.json"),
        {"fixed_terms": {"主城": "Major Hub"}},
    )
    return tmp


def build_translator(work_dir: str, extra: list[str]):
    import translate as main_module

    args = main_module.build_parser().parse_args(["--dir", work_dir, *extra])
    return main_module.Translator(args), main_module


def test_e2e_conflicting_term_is_blocked() -> None:
    """端到端：AI 重复输出已有术语且译法不同 → 不污染 glossary 文件。"""
    tmp = build_workspace()
    try:
        payload = json.dumps(
            {
                "translated_content": "Chapter 1: Lin Yue arrived at the forest.",
                "new_fixed_terms": {"主城": "Main City", "新地名": "New Place"},
                "new_contextual_terms": {"暴毙": {"英文": "die suddenly"}},
            },
            ensure_ascii=False,
        )
        translator, main_module = build_translator(tmp, ["--chapter", "1"])
        translator.client = FakeClient([payload])

        outcome = translator.translate_chapter(1)
        expect(outcome.status == "ok", outcome.message)
        expect(outcome.blocked_terms == 1, f"应拦截 1 条，实际 {outcome.blocked_terms}")

        saved = json.loads(Path(tmp, "glossary_1.json").read_text(encoding="utf-8"))
        expect("主城" not in saved["fixed_terms"], f"冲突词不应写入: {saved['fixed_terms']}")
        expect(saved["fixed_terms"].get("新地名") == "New Place", str(saved["fixed_terms"]))
        expect(saved["contextual_terms"]["暴毙"]["英文"] == "die suddenly")

        conflicts = [json.loads(l) for l in Path(tmp, "glossary_conflicts.jsonl").read_text(encoding="utf-8").splitlines()]
        expect(len(conflicts) == 1 and conflicts[0]["term"] == "主城", str(conflicts))

        # 送进提示词的【术语库】段落必须仍是既定译法
        system_msg = translator.client.calls[0]["messages"][0]["content"]
        glossary_json = system_msg.split("【术语库】", 1)[1].split("【", 1)[0]
        expect("Major Hub" in glossary_json, "术语库段落应是既定译法 Major Hub")
        expect("Main City" not in glossary_json, "术语库段落不应出现被拦截的译法")
    finally:
        shutil.rmtree(tmp)


def test_e2e_rag_block_reaches_the_model() -> None:
    """端到端：--rag 时提示词里出现 <term_context_research> 且含后文片段。"""
    tmp = build_workspace()
    try:
        payload = json.dumps({"translated_content": "Chapter 1 translated."})
        translator, main_module = build_translator(
            tmp, ["--chapter", "1", "--rag", "--rag-terms", "3", "--rag-snippets", "2"]
        )
        translator.client = FakeClient([payload])

        outcome = translator.translate_chapter(1)
        expect(outcome.status == "ok", outcome.message)
        expect(outcome.rag_terms > 0, "应有候选词被检索")

        call = translator.client.calls[0]
        user_msg = call["messages"][1]["content"]
        expect("<term_context_research>" in user_msg, "user 消息应包含检索块")
        expect('word="' in user_msg, "检索块应含 word 属性")
        expect("严禁" in user_msg, "检索块应含禁止剧透的说明")
        # 后文（第 2/3/4 章）里才有的内容应出现在片段中
        expect("云舟说道" in user_msg or "云舟当年" in user_msg or "云舟已经离开" in user_msg,
               "应检索到后文对该词的用法")
    finally:
        shutil.rmtree(tmp)


def test_e2e_rag_off_by_default() -> None:
    tmp = build_workspace()
    try:
        payload = json.dumps({"translated_content": "Chapter 1 translated."})
        translator, main_module = build_translator(tmp, ["--chapter", "1"])
        translator.client = FakeClient([payload])
        translator.translate_chapter(1)
        user_msg = translator.client.calls[0]["messages"][1]["content"]
        expect("term_context_research" not in user_msg, "默认不应在 user 消息里注入检索块")
    finally:
        shutil.rmtree(tmp)


def test_e2e_rag_does_not_write_files_or_break_dry_run() -> None:
    tmp = build_workspace()
    try:
        translator, main_module = build_translator(tmp, ["--chapter", "1", "--rag", "--dry-run"])
        expect(translator.translate_chapter(1).status == "dry-run")
        expect(not Path(tmp, "1_translated.txt").exists())
        expect(not Path(tmp, "glossary_1.json").exists())
        expect(not Path(tmp, "global_glossary_tracker.json").exists())
    finally:
        shutil.rmtree(tmp)


def test_e2e_second_chapter_cannot_override_first() -> None:
    """连续翻译两章：第 2 章的 AI 若改译第 1 章确立的词，必须被拦截。"""
    tmp = build_workspace()
    try:
        first = json.dumps(
            {"translated_content": "Chapter one.", "new_fixed_terms": {"云舟": "Memory Retainer"}},
            ensure_ascii=False,
        )
        second = json.dumps(
            {"translated_content": "Chapter two.", "new_fixed_terms": {"云舟": "Memory Servant"}},
            ensure_ascii=False,
        )
        translator, main_module = build_translator(tmp, ["--chapter", "1-2"])
        translator.client = FakeClient([first, second])

        expect(translator.translate_chapter(1).status == "ok")
        outcome2 = translator.translate_chapter(2)
        expect(outcome2.status == "ok", outcome2.message)
        expect(outcome2.blocked_terms == 1, f"第 2 章应拦截冲突: {outcome2.blocked_terms}")

        saved2 = json.loads(Path(tmp, "glossary_2.json").read_text(encoding="utf-8"))
        expect("云舟" not in saved2["fixed_terms"], str(saved2))

        merged = translator.store.merged_up_to(3)
        expect(merged["fixed_terms"]["云舟"] == "Memory Retainer", str(merged["fixed_terms"]))
    finally:
        shutil.rmtree(tmp)


# ==========================================================================

def main() -> int:
    tests = [
        ("规则: 全局优先于章节", test_global_wins_over_chapter),
        ("规则: merge_glossaries 全局优先", test_merge_glossaries_global_wins),
        ("规则: 语境先到先得（可加新语境）", test_context_first_wins_per_key),
        ("规则: 冲突重复输出被拦截并记录", test_conflicting_reexpression_blocked_and_recorded),
        ("规则: 完全一致的重复输出静默", test_identical_reexpression_is_silent),
        ("规则: 仅大小写差异记为 minor", test_case_only_difference_is_minor),
        ("规则: 重译旧章节不能改写历史", test_retranslating_earlier_chapter_cannot_rewrite_history),
        ("规则: --allow-term-changes 放行章节覆盖", test_allow_term_changes_overrides_chapter_terms),
        ("规则: tracker 的 chapter 与 value 一致", test_tracker_chapter_and_value_consistent),
        ("规则: 净化保留新词并丢弃脏数据", test_sanitize_keeps_new_terms_and_drops_non_strings),
        ("RAG : 单字常见词只带说明", test_ubiquitous_single_char_terms_are_note_only),
        ("RAG : note_only 不检索", test_note_only_candidates_skip_retrieval),
        ("RAG : n-gram 首尾虚词被排除", test_ngrams_with_stop_char_edges_are_not_generated),
        ("术语: 保存章节时保留人工预登记", test_save_chapter_keeps_pre_registered_terms),
        ("术语: 精修不注入本章自有术语", test_merged_before_chapter_excludes_current_terms),
        ("术语: rebuild 时才丢弃旧词条", test_rebuild_glossary_flag_discards_existing),
        ("术语: 作品级优先级最高", test_work_glossary_outranks_global_and_chapters),
        ("术语: reset 重读人工基准", test_reset_reloads_work_glossary),
        ("RAG : 语料统计", test_corpus_index_stats),
        ("RAG : 只检索后文且排除当前章", test_search_future_only_and_excludes_current),
        ("RAG : all 模式后文优先", test_search_all_prefers_future),
        ("RAG : 候选词去碎片", test_extract_candidates_suppresses_fragments),
        ("RAG : 常见词惩罚用真实章节数", test_common_terms_are_penalised_using_true_chapter_count),
        ("RAG : 候选词优先级", test_extract_candidates_priorities),
        ("RAG : 研究结果与渲染", test_researcher_and_render),
        ("RAG : 预算截断", test_render_budget_truncates),
        ("RAG : 无结果时不注入", test_render_empty_when_no_findings),
        ("RAG : 末章无后文", test_search_last_chapter_has_no_future),
        ("端到端: 冲突术语被拦截", test_e2e_conflicting_term_is_blocked),
        ("端到端: RAG 块进入提示词", test_e2e_rag_block_reaches_the_model),
        ("端到端: RAG 默认关闭", test_e2e_rag_off_by_default),
        ("端到端: RAG+dry-run 不写盘", test_e2e_rag_does_not_write_files_or_break_dry_run),
        ("端到端: 第2章不能覆盖第1章", test_e2e_second_chapter_cannot_override_first),
    ]
    for name, func in tests:
        check(name, func)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 66)
    print(f"术语库规则 + RAG 测试: {passed}/{len(RESULTS)} 通过")
    print("=" * 66)
    for name, ok, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print("        " + detail.replace("\n", "\n        "))
    print("=" * 66)
    return 0 if passed == len(RESULTS) else 1


def test_ubiquitous_single_char_terms_are_note_only() -> None:
    """单字且满书都是的词条只带人工说明，不检索片段。

    真实背景：全局 cultural_nuances 里有 '万' 和 '三' 两条单字词条。
    它们在任何一本书里都出现几百次，检索回来的片段全是
    "【售价：10万金】""好一个三无角色" 这种碰巧含该字的噪声，
    却因为 cultural 优先级最高，一次占掉 6 个名额里的 2 个，
    把真正有歧义的词（价值/单位/好人/面包）挤在后面。
    """
    from novelkit import rag

    expect(rag._is_ubiquitous_short("万", 402, 762) is True, "单字+满书常见 → 只带说明")
    expect(rag._is_ubiquitous_short("三", 437, 762) is True)
    expect(rag._is_ubiquitous_short("万", 3, 762) is False, "单字但很少见 → 正常检索")
    expect(rag._is_ubiquitous_short("价值", 167, 762) is False, "多字词不受此规则影响")
    expect(rag._is_ubiquitous_short("万族", 100, 762) is False, "两字词不受此规则影响")

    # 分数必须低于 contextual(1e5)，才会把名额让给真歧义词
    expect(rag.NOTE_ONLY_SCORE < 1e5, "只带说明的词要排在 contextual 之后")
    expect(rag.MAX_NOTE_ONLY >= 1, "仍要保留少量名额给这类词")


def test_ngrams_with_stop_char_edges_are_not_generated() -> None:
    """首尾是虚词的 n-gram 不成词，不应进入候选。

    真实案例："羽泉有希" 旁边会切出 "泉有希的"，"我的心" 会切出 "的心"。
    这类"错位窗口"与正牌词条**互不包含**，所以只查包含关系的
    _suppress_overlaps 抓不到，必须在生成阶段就排除。
    """
    from novelkit import rag

    counts = rag._ngram_counts("羽泉有希的和我的心")
    expect("羽泉有希" in counts, f"正常的专名要保留: {sorted(counts)}")
    expect("泉有希的" not in counts, "末尾带虚词的错位窗口不应生成")
    expect("的心" not in counts, "首字是虚词的不应生成")
    expect(all(g[0] not in rag._BOUNDARY_STOP and g[-1] not in rag._BOUNDARY_STOP
               for g in counts), f"生成的 n-gram 首尾都应是实义字: {sorted(counts)}")
    # 边界规则必须比 _STOP_CHARS 窄，否则会误杀正常词条
    for word in ("云舟", "天使", "将军", "方便", "人才", "可以", "温和"):
        expect(word[0] not in rag._BOUNDARY_STOP and word[-1] not in rag._BOUNDARY_STOP,
               f"边界规则不应误杀 {word}")
    # 关键性质：能构词的实义字不能进边界集合（_STOP_CHARS 里有它们，
    # 所以两个集合不是包含关系，不能简单地断言子集）。
    for ch in "从为以会要说道对我你他大小多少上下个天使将才便和":
        expect(ch not in rag._BOUNDARY_STOP, f"{ch!r} 能构词，不应作为边界虚词")


def test_note_only_candidates_skip_retrieval() -> None:
    """note_only 的候选不该去检索，但仍要带上 localization_note。"""
    from novelkit import rag

    class FakeIndex:
        def __init__(self): self.searched = []
        def chapter_count(self): return 100
        def occurrence_stats(self, term, **kw):
            return (50 if term in ("万", "三") else 2), 500, [1, 2]
        def search(self, term, **kw):
            self.searched.append(term)
            return [rag.Snippet(2, "future", f"…{term}的上下文…")]

    index = FakeIndex()
    glossary = {
        "cultural_nuances": {"万": "表示极多", "三": "常作虚指"},
        "contextual_terms": {"价值": {"x": "value"}},
        "fixed_terms": {},
    }
    text = "万三价值"
    candidates = rag.extract_candidates(text, glossary, index, max_terms=6)
    note_only = [c for c in candidates if c.note_only]
    expect(note_only, f"应当识别出 note_only 候选: {[(c.term, c.score) for c in candidates]}")
    expect(all(c.term in ("万", "三") for c in note_only), str([c.term for c in note_only]))
    # 分数低于 contextual
    contextual = [c for c in candidates if c.source == "contextual"]
    if contextual:
        expect(min(c.score for c in note_only) < min(c.score for c in contextual))


def test_save_chapter_keeps_pre_registered_terms() -> None:
    """翻译一章不能抹掉人工预登记在未来章节的术语。

    真实场景：在面板用「＋指定术语」给第 110 章登记 `老维 → Vik`。
    `merged_up_to(110)` 只读第 0..109 章（**不含 110 自己**），
    所以翻译第 110 章时该条目不在 _estab 里；模型如果不重复提出这个词，
    它就不在净化结果中。以前 save_chapter 整体覆盖 → 人工登记被静默抹掉。
    """
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from novelkit import glossary as G
    from novelkit import text as nktext

    work = Path(tempfile.mkdtemp(prefix="pre_reg_"))
    try:
        (work / "1_origin.txt").write_text("#1", encoding="utf-8")
        nktext.write_json_atomic(str(work / "glossary_110.json"), {
            "fixed_terms": {"老维": "Vik"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})

        store = G.GlossaryStore(str(work), None)
        store.merged_up_to(110)
        expect(store.established("fixed_terms", "老维") is None,
               "第 110 章自己的词条不应进入 _estab（merged_up_to 不含自身）")

        proposed = G.build_chapter_glossary({"new_fixed_terms": {"新路人": "New Passerby"}})
        clean, _ = store.sanitize_chapter(110, proposed)
        store.save_chapter(110, clean)

        after = json.loads((work / "glossary_110.json").read_text(encoding="utf-8"))
        expect("老维" in after["fixed_terms"], f"人工预登记的术语应保留: {after}")
        expect(after["fixed_terms"]["老维"] == "Vik")
        expect("新路人" in after["fixed_terms"], "本次新提取的术语也要写入")

        # 同名键应被新值覆盖（重新翻译能更新译法）
        again = G.build_chapter_glossary({"new_fixed_terms": {"老维": "Old Vik"}})
        clean2, _ = store.sanitize_chapter(110, again)
        store.save_chapter(110, clean2)
        after2 = json.loads((work / "glossary_110.json").read_text(encoding="utf-8"))
        expect(after2["fixed_terms"]["老维"] == "Old Vik", f"同名键应更新: {after2}")
        expect("新路人" in after2["fixed_terms"], "未提到的旧词条仍应保留（合并语义）")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_merged_before_chapter_excludes_current_terms() -> None:
    """精修/就地改写不能把"本章自己确立的术语"当成既定译法注入。

    真实场景：第 1 章第一次翻译时模型自己定了 `本体 → real body`，写进了
    glossary_1.json。用户之后精修这一段，正在表达"这个词应该换个译法"——
    如果把本章词条也注入，模型会以"术语库既定"为由不敢改。
    """
    import shutil
    import tempfile
    from pathlib import Path

    from novelkit import glossary as G
    from novelkit import text as nktext

    work = Path(tempfile.mkdtemp(prefix="merged_before_"))
    try:
        (work / "1_origin.txt").write_text("原来她的本体是做皮肉生意的。", encoding="utf-8")
        nktext.write_json_atomic(str(work / "glossary_1.json"), {
            "fixed_terms": {"本体": "real body"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})
        # 作品级术语（人工基准）必须继续注入
        nktext.write_json_atomic(str(work / "glossary.json"), {
            "fixed_terms": {"暗星": "Dark Star"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})

        chapter = G.GlossaryStore(str(work), None).merged_before_chapter(1)
        base = G.GlossaryStore(str(work), None).merged_up_to(1)
        expect("本体" not in (chapter.get("fixed_terms") or {}),
               f"本章自己的词条不应注入精修: {chapter.get('fixed_terms')}")
        expect("暗星" in (base.get("fixed_terms") or {}),
               "作品级术语是人工基准，必须注入")

        # 精修第 2 章时，第 1 章的既定译法仍然要注入（只有"本章自己"被排除）
        (work / "2_origin.txt").write_text("本体又出现了。", encoding="utf-8")
        for_ch2 = G.GlossaryStore(str(work), None).merged_before_chapter(2)
        expect("本体" in (for_ch2.get("fixed_terms") or {}),
               f"第 1 章的术语对第 2 章仍是既定译法: {for_ch2.get('fixed_terms')}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_rebuild_glossary_flag_discards_existing() -> None:
    """merge=False 时才真正重建：本次没提到的旧词条要被丢弃。

    面板上「重建本章术语」勾选后走的就是这条路（scripts/translate.py --rebuild-glossary）。
    """
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from novelkit import glossary as G
    from novelkit import text as nktext

    work = Path(tempfile.mkdtemp(prefix="rebuild_"))
    try:
        (work / "1_origin.txt").write_text("#1", encoding="utf-8")
        nktext.write_json_atomic(str(work / "glossary_5.json"), {
            "fixed_terms": {"旧词": "Old Term"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})
        store = G.GlossaryStore(str(work), None)
        store.merged_up_to(5)
        clean, _ = store.sanitize_chapter(
            5, G.build_chapter_glossary({"new_fixed_terms": {"新词": "New Term"}}))

        store.save_chapter(5, clean, merge=False)
        after = json.loads((work / "glossary_5.json").read_text(encoding="utf-8"))
        expect("旧词" not in after["fixed_terms"], f"重建应丢弃旧词条: {after}")
        expect(after["fixed_terms"] == {"新词": "New Term"}, str(after))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_work_glossary_outranks_global_and_chapters() -> None:
    """作品级 glossary.json 优先级最高：高于全局、高于所有章节。"""
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from novelkit import glossary as G
    from novelkit import text as nktext

    work = Path(tempfile.mkdtemp(prefix="work_glos_"))
    global_path = work.parent / f"{work.name}_global.json"
    try:
        (work / "1_origin.txt").write_text("#1", encoding="utf-8")
        nktext.write_json_atomic(str(work / "glossary.json"), {
            "fixed_terms": {"地球公司": "Earth Corporation"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})
        nktext.write_json_atomic(str(global_path), {
            "fixed_terms": {"地球公司": "Global Name"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})
        nktext.write_json_atomic(str(work / "glossary_1.json"), {
            "fixed_terms": {"地球公司": "Chapter Name", "本地词": "Local"},
            "contextual_terms": {}, "aesthetic_sentences": {}, "cultural_nuances": {}})

        store = G.GlossaryStore(str(work), str(global_path))
        merged = store.merged_up_to(2)
        expect(merged["fixed_terms"]["地球公司"] == "Earth Corporation",
               f"作品级应胜过全局与章节: {merged['fixed_terms']}")
        expect(store.established("fixed_terms", "地球公司")[0] == G.AUTHORITY_WORK)
        expect(merged["fixed_terms"]["本地词"] == "Local", "章节独有的词仍要保留")

        # 作品级/全局不能被章节覆盖（allow_changes 也不行）
        clean, conflicts = store.sanitize_chapter(2, G.build_chapter_glossary(
            {"new_fixed_terms": {"地球公司": "Hacked"}}), allow_changes=True)
        expect("地球公司" not in clean["fixed_terms"], f"不应写入: {clean}")
        expect(conflicts and conflicts[0]["origin"] == "work", str(conflicts))

        # tracker 要把作品级带上，章节号记 -2，面板据此显示「作品」
        tracker = json.loads(Path(store.write_tracker()).read_text(encoding="utf-8"))
        expect(tracker["fixed_terms"]["地球公司"]["chapter"] == G.AUTHORITY_WORK,
               str(tracker["fixed_terms"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        global_path.unlink(missing_ok=True)


def test_reset_reloads_work_glossary() -> None:
    """reset() 必须重读作品级/全局基准。

    否则面板刚写进作品术语库的词条在本进程内还是看不见，
    紧接着重建 tracker 就会漏掉它（实测踩过）。
    """
    import shutil
    import tempfile
    from pathlib import Path

    from novelkit import glossary as G
    from novelkit import text as nktext

    work = Path(tempfile.mkdtemp(prefix="reset_reload_"))
    try:
        (work / "1_origin.txt").write_text("#1", encoding="utf-8")
        store = G.GlossaryStore(str(work), None)
        expect(store.work_glossary["fixed_terms"] == {})

        # 模拟面板写入作品术语库**之后**才调 reset()
        nktext.write_json_atomic(str(work / "glossary.json"), {
            "fixed_terms": {"地球公司": "Earth Corporation"}, "contextual_terms": {},
            "aesthetic_sentences": {}, "cultural_nuances": {}})
        store.reset()
        expect(store.work_glossary["fixed_terms"].get("地球公司") == "Earth Corporation",
               "reset 后应能读到新写入的作品术语")
        store.write_tracker()
        import json
        tracker = json.loads((work / "global_glossary_tracker.json").read_text(encoding="utf-8"))
        expect("地球公司" in tracker["fixed_terms"], f"tracker 应包含它: {tracker['fixed_terms']}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
