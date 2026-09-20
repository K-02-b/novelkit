"""RAG 术语上下文检索。

用途
----
模型在翻译时只看得到当前章节和少量前后文，遇到"可能有特定内涵"的词
（成语/黑话/网络用语/自造名词/一词多义）很容易选错义项。本模块允许在翻译前
自动挑出这类候选词，回到全书原文里检索它们**在别处（尤其是后文）**的使用语境，
再把这些片段作为参考资料喂给模型，帮助它选定合适译法。

候选词来源（按优先级）
--------------------
  1. cultural_nuances —— 人工整理的文化/俚语词条，天然是"有特定内涵"的词；
  2. contextual_terms —— 历史翻译中被判定为"需按语境选择"的词；
  3. 新词发现 —— 当前章节中反复出现的 2~6 字中文片段，若全书也反复出现，
     多半是专有名词或关键概念，而它们的译法会影响之后所有章节。

设计要点
--------
* 语料只加载一次（进程内缓存）。首次检索时把 work_dir 下所有 `*_origin.txt`
  读进内存；以 two/ 的 766 章计算约 5~6 MB，可接受。
* 检索按 scope 排序：默认只看**后文**（用户要求的"检索后文该词的上下文"），
  也可切换 past / all。
* 片段按句子边界切分，避免把半句话喂给模型。
* 结果有字符预算上限，防止把提示词撑爆。
* 明确告知模型：这些片段**仅供消歧**，严禁翻译/复述，严禁剧透后文情节。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import text as nktext

# ---------------------------------------------------------------------------
# 候选词抽取
# ---------------------------------------------------------------------------

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 高频虚词/功能字：一个 n-gram 若全部由这些字组成，则不作为候选
_STOP_CHARS = set(
    "的了是在我你他她它们这那有和与就都也很不会要说道着过被把给对从而但因所为以之其于"
    "却又还只才便让使得个上下来去多少好大中小吗呢吧啊哦嗯呀噢嘛啦"
)

# 只用于判断"词条首尾边界"的虚词集合。
# 注意不能复用 _STOP_CHARS：那是用来判断"整串是否全是虚词"的，里面含
# 我/你/他/大/小/多/少/上/下/从/为/以 这类**能构词**的实义字，
# 拿它做边界会把 "记忆""天使""将军""方便""人才" 这类正常词条误杀。
# 这里只保留真正的结构助词、语气词与并列连词。
_BOUNDARY_STOP = set("的了着过地得吗呢吧啊哦嗯呀噢嘛啦之其而与及或")

_SENTENCE_END = "。！？!?；;…\n"


@dataclass
class Candidate:
    term: str
    source: str  # cultural | contextual | discovered
    in_chapter: int = 0
    score: float = 0.0
    chapters: List[int] = field(default_factory=list)
    total: int = 0
    note_only: bool = False   # 只带人工说明、不检索片段（见 _is_ubiquitous_short）

    def __hash__(self) -> int:
        return hash(self.term)


# 单字且满书都是的词（"万""三"这类）：人工写下的文化说明仍然有价值，
# 但检索回来的片段几乎全是 "10万金""三无角色" 这种碰巧含该字的噪声，
# 白白占掉检索名额、还会把真正的歧义词挤出去。所以这类词降权、只带说明不检索，
# 并且数量设上限。
NOTE_ONLY_SCORE = 5e4          # 低于 contextual(1e5)，把名额让给真歧义词
MAX_NOTE_ONLY = 2
UBIQUITOUS_CHAPTER_RATIO = 0.3


def _is_ubiquitous_short(term: str, chapter_hits: int, total_chapters: int) -> bool:
    """是否为"单字 + 在全书里太普遍"的词条。"""
    return len(term) <= 1 and chapter_hits >= max(8, UBIQUITOUS_CHAPTER_RATIO * total_chapters)


def _is_meaningful(gram: str) -> bool:
    if len(gram) < 2:
        return False
    # 至少含一个实义字
    return any(ch not in _STOP_CHARS for ch in gram)


def _ngram_counts(chapter_text: str, *, min_len: int = 2, max_len: int = 6) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for run in _CJK_RUN.findall(chapter_text):
        length = len(run)
        for n in range(min_len, max_len + 1):
            if n > length:
                break
            for i in range(length - n + 1):
                gram = run[i : i + n]
                if not _is_meaningful(gram):
                    continue
                # 首尾是虚词的片段不成词，多半是相邻词的"错位窗口"：
                # 例如 "羽泉有希" 旁边切出的 "泉有希的"、由 "我的心" 切出的 "的心"。
                # 它们与正牌词条互不包含，靠下面的 _suppress_overlaps（只查包含关系）
                # 抓不到，所以在这里就不生成。
                if gram[0] in _BOUNDARY_STOP or gram[-1] in _BOUNDARY_STOP:
                    continue
                counts[gram] = counts.get(gram, 0) + 1
    return counts


def _suppress_overlaps(candidates: List[Candidate]) -> List[Candidate]:
    """去掉互为子串的候选，避免同时喂 "苍穹神" 和 "苍穹神木"。

    candidates 需按 score 降序传入；同族只保留分更高（通常是更长）的那个。
    """
    kept: List[Candidate] = []
    for candidate in candidates:
        if any(candidate.term in k.term or k.term in candidate.term for k in kept):
            continue
        kept.append(candidate)
    return kept


def extract_candidates(
    chapter_text: str,
    glossary: Dict[str, Dict],
    index: "CorpusIndex",
    *,
    max_terms: int = 6,
) -> List[Candidate]:
    """从当前章节挑出值得检索上下文的候选词。"""
    cultural = glossary.get("cultural_nuances") or {}
    contextual = glossary.get("contextual_terms") or {}
    fixed = glossary.get("fixed_terms") or {}

    picked: Dict[str, Candidate] = {}
    total_chapters = max(1, index.chapter_count())

    # --- 1. 人工整理的文化/俚语词条（最高优先级）
    #     但"单字 + 满书都是"的要降权并只保留说明，否则 "万""三" 会占满检索名额。
    note_only_used = 0
    for term in cultural:
        if not term or term not in chapter_text or term in fixed:
            continue
        hits, total, sample = index.occurrence_stats(term)
        if _is_ubiquitous_short(term, hits, total_chapters):
            if note_only_used >= MAX_NOTE_ONLY:
                continue          # 名额有限，够用即可
            note_only_used += 1
            picked[term] = Candidate(term, "cultural", chapter_text.count(term),
                                     NOTE_ONLY_SCORE, sample, total, note_only=True)
        else:
            picked[term] = Candidate(term, "cultural", chapter_text.count(term), 1e6)

    # --- 2. 历史上被判定为"需按语境选择"的词
    # 注意：已有 fixed 译法的词无需检索——译法已经定了，没有"选择"的余地。
    for term in contextual:
        if not term or term in fixed or term not in chapter_text:
            continue
        existing = picked.get(term)
        if existing is None:
            picked[term] = Candidate(term, "contextual", chapter_text.count(term), 1e5)
        else:
            existing.source = "contextual"
            existing.score = 1e5

    # --- 3. 新词发现
    counts = _ngram_counts(chapter_text)
    discovered: List[Candidate] = []
    for gram, count in counts.items():
        if gram in picked or gram in fixed:
            continue  # 已定译法的词不需要再检索
        threshold = 2 if len(gram) >= 3 else 3
        if count < threshold:
            continue
        # 长度越大越可能是完整词条，给一点加成
        discovered.append(Candidate(gram, "discovered", count, count * (1.0 + 0.35 * (len(gram) - 2))))
    discovered.sort(key=lambda c: c.score, reverse=True)
    discovered = _suppress_overlaps(discovered[: max_terms * 4])

    # 计算语料统计后再排序（只对候选池里的词做，控制开销）
    scored: List[Candidate] = []
    for candidate in discovered:
        chapter_hits, total, sample = index.occurrence_stats(candidate.term)
        candidate.chapters = sample
        candidate.total = total

        # 全书到处都是的词不是"有特定内涵"的词，直接淘汰，别浪费检索名额。
        # 阈值带绝对下限 8，避免小语料里把每个词都判成常见词。
        if chapter_hits >= max(8, 0.5 * total_chapters):
            continue

        candidate.score += chapter_hits * 2.5
        if chapter_hits >= 2:
            candidate.score += 5  # 跨章反复出现 → 很可能是关键概念
        if chapter_hits >= max(4, 0.35 * total_chapters):
            candidate.score -= 30  # 偏常见，降权
        if chapter_hits <= 1:
            candidate.score -= 8  # 只在当前章出现，检索不到有用上下文
        scored.append(candidate)

    discovered = scored
    discovered.sort(key=lambda c: c.score, reverse=True)
    discovered = _suppress_overlaps(discovered)

    ordered = sorted(picked.values(), key=lambda c: c.score, reverse=True)
    ordered.extend(discovered)
    # 关键：必须在"已知词 + 新发现词"合并之后再统一去碎片，
    # 否则 contextual 里的 "苍穹神木" 会和自动发现的碎片 "苍穹神"、"穹神木" 一起送出去。
    ordered = _suppress_overlaps(ordered)
    return ordered[:max_terms]


# ---------------------------------------------------------------------------
# 语料索引
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snippet:
    chapter: int
    side: str  # future | past
    text: str


def _snippet_around(text: str, position: int, length: int, window: int) -> str:
    """以出现位置为中心，向两侧扩展到句子边界。"""
    start = position
    left_limit = max(0, position - window)
    while start > left_limit and text[start - 1] not in _SENTENCE_END:
        start -= 1

    end = position + length
    right_limit = min(len(text), position + length + window)
    while end < right_limit and text[end] not in _SENTENCE_END:
        end += 1
    if end < len(text) and text[end] in _SENTENCE_END:
        end += 1

    fragment = text[start:end]
    fragment = re.sub(r"\s+", " ", fragment).strip()
    if start > 0:
        fragment = "…" + fragment
    if end < len(text):
        fragment = fragment + "…"
    return fragment


class CorpusIndex:
    """全书原文索引：一次加载，多次检索。"""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self._items: Optional[List[Tuple[int, str]]] = None
        self._stats_cache: Dict[str, Tuple[int, int]] = {}

    # ------------------------------------------------------------- 加载

    def ensure(self) -> List[Tuple[int, str]]:
        if self._items is None:
            items: List[Tuple[int, str]] = []
            for number in nktext.chapter_files(self.work_dir, "_origin.txt"):
                content = nktext.read_text(
                    os.path.join(self.work_dir, f"{number}_origin.txt"), ""
                )
                if content and content.strip():
                    items.append((number, content))
            self._items = items
        return self._items

    def invalidate(self) -> None:
        self._items = None
        self._stats_cache.clear()

    def chapter_count(self) -> int:
        return len(self.ensure())

    @property
    def chapters(self) -> List[int]:
        return [number for number, _ in self.ensure()]

    def total_chars(self) -> int:
        return sum(len(text) for _, text in self.ensure())

    # ------------------------------------------------------------- 统计

    def stats(self, term: str, *, max_matches: int = 100000) -> Tuple[int, int]:
        """返回 (出现的章节数, 出现总次数)。结果带缓存。"""
        chapter_count, total, _ = self.occurrence_stats(term, max_matches=max_matches)
        return chapter_count, total

    def occurrence_stats(
        self, term: str, *, sample_chapters: int = 24, max_matches: int = 100000
    ) -> Tuple[int, int, List[int]]:
        """返回 (真实出现章节数, 出现总次数, 章节号样本[最多 sample_chapters 个])。

        章节数必须是**真实值**而不是样本长度：候选打分要用它判断"这个词是不是
        在全书里太普遍"。若被样本上限截断，像"记忆"这种满书都是的词
        会被误判成稀有词，反而拿到高分。
        """
        if not term:
            return 0, 0, []
        cached = self._stats_cache.get(term)
        if cached is not None:
            return cached

        chapter_count = 0
        sample: List[int] = []
        total = 0
        for number, content in self.ensure():
            if term not in content:
                continue
            chapter_count += 1
            if len(sample) < sample_chapters:
                sample.append(number)
            total += content.count(term)
            if total > max_matches:
                break
        result = (chapter_count, total, sample)
        self._stats_cache[term] = result
        return result

    def chapters_containing(self, term: str, limit: int = 24) -> List[int]:
        return self.occurrence_stats(term, sample_chapters=limit)[2]

    # ------------------------------------------------------------- 检索

    def search(
        self,
        term: str,
        *,
        current: int,
        limit: int = 2,
        window: int = 100,
        scope: str = "future",
    ) -> List[Snippet]:
        """检索 term 在别处的上下文片段。

        scope: future（只看后文，默认）/ past（只看前文）/ all（后文优先，其次前文）
        """
        if not term or limit <= 0:
            return []

        items = self.ensure()
        future = [(n, t) for n, t in items if n > current]
        past = [(n, t) for n, t in items if n < current]

        if scope == "past":
            ordered = list(reversed(past))
        elif scope == "all":
            ordered = future + list(reversed(past))
        else:
            ordered = future

        snippets: List[Snippet] = []
        seen = set()
        for number, content in ordered:
            if len(snippets) >= limit:
                break
            side = "future" if number > current else "past"
            start = 0
            while len(snippets) < limit:
                position = content.find(term, start)
                if position < 0:
                    break
                fragment = _snippet_around(content, position, len(term), window)
                start = position + len(term)
                fingerprint = (number, fragment[:60])
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                snippets.append(Snippet(number, side, fragment))
        return snippets


# ---------------------------------------------------------------------------
# 研究结果
# ---------------------------------------------------------------------------


@dataclass
class TermResearch:
    term: str
    source: str
    note: Optional[str] = None
    existing: Dict[str, str] = field(default_factory=dict)
    chapters: List[int] = field(default_factory=list)
    total: int = 0
    snippets: List[Snippet] = field(default_factory=list)


class TermResearcher:
    """把"候选词 → 检索片段"串起来的门面。"""

    def __init__(self, work_dir: str, *, scope: str = "future", window: int = 100,
                 snippets_per_term: int = 2, max_terms: int = 6, budget: int = 6000):
        self.index = CorpusIndex(work_dir)
        self.scope = scope
        self.window = window
        self.snippets_per_term = snippets_per_term
        self.max_terms = max_terms
        self.budget = budget

    def research(
        self,
        chapter_text: str,
        glossary: Dict[str, Dict],
        *,
        current_chapter: int,
    ) -> List[TermResearch]:
        candidates = extract_candidates(
            chapter_text,
            glossary,
            self.index,
            max_terms=self.max_terms,
        )

        cultural = glossary.get("cultural_nuances") or {}
        contextual = glossary.get("contextual_terms") or {}

        results: List[TermResearch] = []
        for candidate in candidates:
            # 只带说明的词不检索：它的片段必然是同字不同义的噪声
            snippets = [] if candidate.note_only else self.index.search(
                candidate.term,
                current=current_chapter,
                limit=self.snippets_per_term,
                window=self.window,
                scope=self.scope,
            )
            # 新发现的词若检索不到任何别处上下文，就没有检索价值
            if not snippets and candidate.source == "discovered":
                continue

            _, total, sample = self.index.occurrence_stats(candidate.term)
            note = cultural.get(candidate.term)
            contexts = contextual.get(candidate.term)
            results.append(
                TermResearch(
                    term=candidate.term,
                    source=candidate.source,
                    note=note if isinstance(note, str) else None,
                    existing=dict(contexts) if isinstance(contexts, dict) else {},
                    chapters=[n for n in sample if n != current_chapter][:12],
                    total=total,
                    snippets=snippets,
                )
            )
        return results[: self.max_terms]

    # ------------------------------------------------------------- 渲染

    def render(self, results: Sequence[TermResearch]) -> str:
        """渲染成注入提示词的 XML 片段；超出预算则截断。"""
        if not results:
            return ""

        lines: List[str] = [
            "<term_context_research>",
            "  <!-- 系统自动检索出的候选词上下文，仅供判断词义与选定译法使用。",
            "       严禁翻译或复述以下片段；严禁把后文情节、伏笔、结局提前写进译文。 -->",
        ]
        used = sum(len(line) for line in lines)
        truncated = False

        for item in results:
            attrs = [f'word="{_xml_escape(item.term)}"', f'source="{item.source}"']
            if not item.snippets and item.note:
                # 说明这类词为什么没有片段，免得模型以为检索失败
                attrs.append('note_only="true"')
            if item.total:
                attrs.append(f'occurrences="{item.total}"')
            block = [f"  <term {' '.join(attrs)}>"]
            used += len(block[0])

            if item.note:
                text = _clip(item.note, 300)
                block.append(f"    <localization_note>{_xml_escape(text)}</localization_note>")
            for ctx_key, ctx_val in list(item.existing.items())[:4]:
                block.append(
                    f'    <established_context context="{_xml_escape(_clip(ctx_key, 40))}">'
                    f"{_xml_escape(_clip(ctx_val, 120))}</established_context>"
                )
            for snippet in item.snippets:
                body = _clip(snippet.text, 320)
                block.append(
                    f'    <{snippet.side} chapter="{snippet.chapter}">{_xml_escape(body)}</{snippet.side}>'
                )
            block.append("  </term>")

            block_chars = sum(len(line) for line in block)
            if used + block_chars > self.budget:
                truncated = True
                break
            used += block_chars
            lines.extend(block)

        if truncated:
            lines.append("  <!-- 预算已满，部分候选词的检索结果被省略 -->")
        lines.append("</term_context_research>")
        return "\n".join(lines)


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
