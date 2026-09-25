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
  3. **模型提名**（proposed，可选）—— 翻译前额外调一次模型，让它自己列出
     "值得先查全书语境"的词；解析时会校验"必须原样出现在本章、长度 2~8、
     不在 fixed_terms 里"，防止模型凭空造词。开关见 `--rag-ask` / `--no-rag-ask`。
  4. 新词发现 —— 当前章节中反复出现的 2~6 字中文片段，需同时满足：
       * **凝固度**（cohesion）：各切分点的 `count(g)*N/(count(left)*count(right))`
         都足够高，说明这几个字是"抱团"的，而不是跨词边界的偶然拼接；
       * **自由度**（boundary entropy）：左右邻接字至少要有一侧是分散的
         （不能两侧都固定），说明它能出现在不同上下文里，是一个独立的词，
         而不是某个更长词条的固定残余片段；
       * 中间夹着"的了着过"这类结构助词的 3 字以上窗口直接排除（那是短语）；
       * 全书并不普遍（否则就是"记忆""时候"这类常见词）。
     这是经典的无监督新词发现做法，能显著压低 `苍穹神`/`穹神木`/`神木` 这类碎片。

打分还会额外考虑（都只是排序信号，不影响正确性）
------------------------------------------------
  * 跨章反复出现 → 很可能是关键概念；
  * **首次出现在当前章节** → 新概念，此章就要把译法定下来，优先级最高；
  * 命中"叫做/称为/是指/意味着"等定义句式 → 该词正在被解释，译法影响后续所有章节；
  * 全书到处都是的词即使凝固度/自由度合格也直接淘汰。

设计要点
--------
* 语料只加载一次（进程内缓存）。首次检索时把 work_dir 下所有 `*_origin.txt`
  读进内存；以 two/ 的 766 章计算约 5~6 MB，可接受。
* 检索按 scope 排序：默认只看**后文**（用户要求的"检索后文该词的上下文"），
  也可切换 past / all。
* 片段按句子边界切分，避免把半句话喂给模型；优先取"有解释性"的句子，
  并尽量让片段分散在不同章节里。
* 结果有字符预算上限，且预算按词条**均摊**：预算紧张时缩短每条片段，
  而不是把排在后面的候选词整块丢掉。
* 明确告知模型：这些片段**仅供消歧**，严禁翻译/复述，严禁剧透后文情节。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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

# 新词发现参数
MAX_TERM_LEN = 6              # 候选词最大长度（字）
MIN_FREQ_LONG = 2            # >=3 字词在当前章节的最少出现次数
MIN_FREQ_SHORT = 3           # 2 字词在当前章节的最少出现次数
DISCOVERY_POOL = 48          # 送去做全书统计的候选池上限（控制 CPU）
MIN_COHESION = 2.0           # 凝固度下限：低于它多半是跨词边界的偶然片段
MIN_FREEDOM = 0.5            # 左右邻接熵下限：低于它说明上下文过于固定
MAX_BOUNDARY_SAMPLES = 400   # 每个词最多采样多少次邻字
PROFILE_SAMPLE_CAP = 64      # 缓存的章节号样本上限

# 定义句式：命中说明该词正在被解释，值得优先定译法
_DEFINITION_MARKERS = (
    "叫做", "叫作", "称为", "称作", "名为", "号称", "所谓", "又叫",
    "也就是", "指的是", "全称", "是指", "即是", "意味着", "简称",
)


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


def _fragment_of_known(gram: str, known) -> bool:
    """gram 是否是某个已知词条（fixed/已选候选）的一部分。

    只判断"被包含"，不判断"包含"：否则 note_only 的单字词条（万/三）会把
    "三无角色"这类真正值得检索的词一起误杀。
    """
    return any(gram in key for key in known if key)


def _ngram_counts(chapter_text: str, *, min_len: int = 2, max_len: int = MAX_TERM_LEN) -> Dict[str, int]:
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
                # 中间夹着结构助词/语气词的 3 字以上片段是短语而不是词条：
                # "石镇住了亡魂" 这类跨句子成分的窗口也应排除。
                if len(gram) >= 3 and any(ch in _BOUNDARY_STOP for ch in gram[1:-1]):
                    continue
                counts[gram] = counts.get(gram, 0) + 1
    return counts


def _raw_ngram_counts(chapter_text: str, *, max_len: int = MAX_TERM_LEN) -> Dict[str, int]:
    """不做任何过滤的 n-gram 计数，供凝固度计算使用。

    这里必须保留全部片段（含虚词、含短片段），否则计算 `count(left)`、
    `count(right)` 时会出现假 0，把正常词的凝固度算崩。
    """
    counts: Dict[str, int] = {}
    for run in _CJK_RUN.findall(chapter_text):
        length = len(run)
        for n in range(1, max_len + 1):
            if n > length:
                break
            for i in range(length - n + 1):
                gram = run[i : i + n]
                counts[gram] = counts.get(gram, 0) + 1
    return counts


def _cohesion(gram: str, counts: Dict[str, int], total_chars: int) -> float:
    """凝固度：所有切分点里最弱的那一个。

    `count(g)*N / (count(left)*count(right))` 越大，说明 g 越不可能是
    left/right 偶然相邻拼出来的，而是一个稳定的整体。
    """
    if len(gram) < 2 or total_chars <= 0:
        return 0.0
    gram_count = counts.get(gram, 0)
    if gram_count <= 0:
        return 0.0
    best: Optional[float] = None
    for i in range(1, len(gram)):
        left, right = gram[:i], gram[i:]
        left_count = counts.get(left, 0)
        right_count = counts.get(right, 0)
        if left_count <= 0 or right_count <= 0:
            continue
        value = gram_count * total_chars / (left_count * right_count)
        best = value if best is None else min(best, value)
    return best or 0.0


def _entropy(counter: Counter) -> float:
    """离散分布的香农熵（自然对数）。空分布返回 0。"""
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    result = 0.0
    for value in counter.values():
        if value <= 0:
            continue
        p = value / total
        result -= p * math.log(p)
    return result


def _looks_fixed_context(profile: "TermProfile") -> bool:
    """左右邻字都几乎没有变化 → 固定搭配里的跨词片段，不是独立词条。

    只要求"至少一侧自由"：网文里 "镇魂塔的……" 这类真词条也会有固定的一侧，
    一刀切要求两侧都自由会把它们误杀。
    """
    return max(_entropy(profile.left), _entropy(profile.right)) < MIN_FREEDOM


def _definition_bonus(text: str, term: str, *, window: int = 12, max_hits: int = 12) -> float:
    """命中"叫做 X / X 指的是 / X 是……"这类定义句式时给一点加分。"""
    bonus = 0.0
    seen = 0
    start = 0
    while seen < max_hits:
        position = text.find(term, start)
        if position < 0:
            break
        start = position + len(term)
        seen += 1
        left = text[max(0, position - window) : position]
        right = text[position + len(term) : position + len(term) + window]
        if any(marker in left for marker in _DEFINITION_MARKERS):
            bonus += 10.0
        if right.startswith(("是", "指", "即", "，是")):
            bonus += 10.0
        if bonus >= 30.0:
            break
    return bonus


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


def _discover_candidates(
    chapter_text: str,
    index: "CorpusIndex",
    *,
    known,
    current_chapter: int,
    total_chapters: int,
) -> List[Candidate]:
    """在当前章节里做无监督新词发现，返回按分数降序的候选。"""
    counts = _ngram_counts(chapter_text)
    if not counts:
        return []

    raw = _raw_ngram_counts(chapter_text)
    total_chars = sum(len(run) for run in _CJK_RUN.findall(chapter_text)) or 1

    pool: List[Tuple[float, str, int]] = []
    for gram, count in counts.items():
        if _fragment_of_known(gram, known):
            continue
        threshold = MIN_FREQ_LONG if len(gram) >= 3 else MIN_FREQ_SHORT
        if count < threshold:
            continue
        # 凝固度是纯本地计算，先用它挡掉大量碎片，再进昂贵的全书统计。
        if _cohesion(gram, raw, total_chars) < MIN_COHESION:
            continue
        # 长度越大越可能是完整词条，给一点加成
        base = count * (1.0 + 0.35 * (len(gram) - 2))
        pool.append((base, gram, count))

    pool.sort(key=lambda item: item[0], reverse=True)
    pool = pool[:DISCOVERY_POOL]

    common_limit = max(8, 0.5 * total_chapters)
    very_common = max(4, 0.35 * total_chapters)
    scored: List[Candidate] = []
    for base, gram, count in pool:
        profile = index.term_profile(gram)

        # 全书到处都是的词不是"有特定内涵"的词，直接淘汰，别浪费检索名额。
        # 阈值带绝对下限 8，避免小语料里把每个词都判成常见词。
        if profile.chapters >= common_limit:
            continue

        # 自由度：两侧邻字至少要有一侧是分散的。
        # 注意不能要求"两侧都必须分散"：网文里 "镇魂塔的……" 这类真词条也会有
        # 一侧固定（右边永远是"的"），一刀切会把它们误杀。真正要挡的是
        # "塔的镇"这种左右都固定、只可能出现在一个固定搭配里的跨词边界碎片，
        # 以及只出现一两次、根本没有语境的词。
        if _looks_fixed_context(profile):
            continue

        left_entropy = _entropy(profile.left)
        right_entropy = _entropy(profile.right)
        score = base
        score += profile.chapters * 2.5
        # 自由度越高越像一个独立的词（只做排序，不再做硬性淘汰）
        score += min(1.5, min(left_entropy, right_entropy)) * 4.0
        if profile.chapters >= 2:
            score += 5  # 跨章反复出现 → 很可能是关键概念
        if profile.chapters >= very_common:
            score -= 30  # 偏常见，降权
        if profile.chapters <= 1:
            score -= 8  # 只在当前章出现，检索不到有用上下文
        if current_chapter >= 0 and profile.first == current_chapter:
            score += 12  # 首次登场的新概念，本章就要把译法定下来
        score += _definition_bonus(chapter_text, gram)

        scored.append(
            Candidate(
                gram, "discovered", count, score,
                list(profile.sample), profile.total,
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def extract_candidates(
    chapter_text: str,
    glossary: Dict[str, Dict],
    index: "CorpusIndex",
    *,
    max_terms: int = 10,
    current_chapter: int = -1,
    extra_terms: Sequence[str] = (),
) -> List[Candidate]:
    """从当前章节挑出值得检索上下文的候选词。

    current_chapter 用于识别"首次出现在本章"的新概念；不传则跳过该加分项。
    extra_terms 是外部（通常是模型自己）提名的词，会作为 proposed 高优先级候选并入。
    """
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

    # --- 3. 模型提名（外部传入；这里再校验一次，防模型凭空造词）
    for term in extra_terms or ():
        term = _clean_term(term)
        if not term or term in fixed or term not in chapter_text:
            continue
        if len(term) < 2 or term in picked:
            continue
        picked[term] = Candidate(term, "proposed", chapter_text.count(term), PROPOSED_SCORE)

    # --- 4. 新词发现（凝固度 + 自由度 + 全书统计）
    known = set(picked) | set(fixed)
    discovered = _discover_candidates(
        chapter_text,
        index,
        known=known,
        current_chapter=current_chapter,
        total_chapters=total_chapters,
    )

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


@dataclass
class TermProfile:
    """一个词在全书的统计画像（只统计一次，缓存复用）。"""

    chapters: int = 0
    total: int = 0
    sample: List[int] = field(default_factory=list)   # 升序、最多 PROFILE_SAMPLE_CAP 个
    first: int = -1
    last: int = -1
    left: Counter = field(default_factory=Counter)    # 左邻字分布
    right: Counter = field(default_factory=Counter)   # 右邻字分布


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


def _snippet_priority(text: str) -> int:
    """越像"在解释这个词"的句子越优先取。"""
    score = 0
    if any(marker in text for marker in _DEFINITION_MARKERS):
        score += 2
    if "：" in text or ":" in text:
        score += 1
    return score


class CorpusIndex:
    """全书原文索引：一次加载，多次检索。"""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self._items: Optional[List[Tuple[int, str]]] = None
        self._profile_cache: Dict[str, TermProfile] = {}

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
        self._profile_cache.clear()

    def chapter_count(self) -> int:
        return len(self.ensure())

    @property
    def chapters(self) -> List[int]:
        return [number for number, _ in self.ensure()]

    def total_chars(self) -> int:
        return sum(len(text) for _, text in self.ensure())

    # ------------------------------------------------------------- 统计

    def term_profile(self, term: str) -> TermProfile:
        """扫描全书，返回 (章节数/总次数/章节样本/首末章/左右邻字分布)。

        整本书只扫一遍，结果缓存；`occurrence_stats` 与候选打分共用同一份画像。
        """
        if not term:
            return TermProfile()
        cached = self._profile_cache.get(term)
        if cached is not None:
            return cached

        profile = TermProfile()
        boundary_samples = 0
        for number, content in self.ensure():
            if term not in content:
                continue
            profile.chapters += 1
            if profile.first < 0:
                profile.first = number
            profile.last = number
            if len(profile.sample) < PROFILE_SAMPLE_CAP:
                profile.sample.append(number)
            profile.total += content.count(term)
            if boundary_samples >= MAX_BOUNDARY_SAMPLES:
                continue
            start = 0
            while boundary_samples < MAX_BOUNDARY_SAMPLES:
                position = content.find(term, start)
                if position < 0:
                    break
                left = content[position - 1] if position > 0 else "\x00"
                end = position + len(term)
                right = content[end] if end < len(content) else "\x00"
                profile.left[left] += 1
                profile.right[right] += 1
                boundary_samples += 1
                start = end

        self._profile_cache[term] = profile
        return profile

    def occurrence_stats(
        self, term: str, *, sample_chapters: int = 24, max_matches: int = 100000
    ) -> Tuple[int, int, List[int]]:
        """返回 (真实出现章节数, 出现总次数, 章节号样本[最多 sample_chapters 个])。

        章节数必须是**真实值**而不是样本长度：候选打分要用它判断"这个词是不是
        在全书里太普遍"。若被样本上限截断，像"记忆"这种满书都是的词
        会被误判成稀有词，反而拿到高分。
        """
        profile = self.term_profile(term)
        return profile.chapters, profile.total, profile.sample[:sample_chapters]

    def stats(self, term: str, *, max_matches: int = 100000) -> Tuple[int, int]:
        """返回 (出现的章节数, 出现总次数)。结果带缓存。"""
        chapter_count, total, _ = self.occurrence_stats(term, max_matches=max_matches)
        return chapter_count, total

    def chapters_containing(self, term: str, limit: int = 24) -> List[int]:
        return self.occurrence_stats(term, sample_chapters=limit)[2]

    # ------------------------------------------------------------- 检索

    def _best_snippet(
        self, number: int, content: str, term: str, side: str, window: int
    ) -> Optional[Snippet]:
        """一章内优先取"最像在解释这个词"的那一处。"""
        best: Optional[Snippet] = None
        best_priority = -1
        start = 0
        while True:
            position = content.find(term, start)
            if position < 0:
                break
            start = position + len(term)
            text = _snippet_around(content, position, len(term), window)
            priority = _snippet_priority(text)
            if priority > best_priority:
                best_priority = priority
                best = Snippet(number, side, text)
        return best

    def search(
        self,
        term: str,
        *,
        current: int,
        limit: int = 3,
        window: int = 100,
        scope: str = "future",
    ) -> List[Snippet]:
        """检索 term 在别处的上下文片段。

        scope: future（只看后文，默认）/ past（只看前文）/ all（后文优先，其次前文）
        片段尽量分散在不同章节：第一轮每章取一条，不够再回头补同章的其它用法。
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

        # 第一轮：每个章节最多一条，保证检索到的语境尽量分散
        for number, content in ordered:
            if len(snippets) >= limit:
                break
            side = "future" if number > current else "past"
            snippet = self._best_snippet(number, content, term, side, window)
            if snippet is None:
                continue
            fingerprint = snippet.text[:40]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            snippets.append(snippet)

        # 第二轮：后文/前文章节不够时，再从同一章节里补充其它用法
        if len(snippets) < limit:
            for number, content in ordered:
                if len(snippets) >= limit:
                    break
                side = "future" if number > current else "past"
                start = 0
                while len(snippets) < limit:
                    position = content.find(term, start)
                    if position < 0:
                        break
                    start = position + len(term)
                    text = _snippet_around(content, position, len(term), window)
                    fingerprint = text[:40]
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    snippets.append(Snippet(number, side, text))
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


# 渲染时每个词条至少保留的字符数，避免预算被前几个词吃光
MIN_TERM_BLOCK = 80
MAX_TERM_BLOCK = 1400


# ---------------------------------------------------------------------------
# 让模型自己提名关键词（可选的一步：调一次模型，把它的提名也放进检索）
# ---------------------------------------------------------------------------

PROPOSAL_LIMIT = 12      # 模型每次最多提名多少个词
PROPOSED_SCORE = 3e5     # 低于人工 cultural(1e6)，高于历史 contextual(1e5)

_PROPOSAL_SYSTEM = (
    "你是小说翻译的术语抽取助手。请阅读用户给出的中文小说章节，挑出**真正值得先查全书语境再定译法**的词。\n"
    "值得挑：自造专名（人名/地名/门派/法宝/组织/种族）、有特定内涵的成语黑话与网络用语、"
    "一词多义且译法会影响后文的词。\n"
    "不要挑：常见虚词与普通名词、读者凭常识就能理解的词、已经在【已知术语】里的词。\n"
    "每个词都必须**原样出现在章节正文里**，长度 2~8 个汉字，不要输出解释或例句。\n"
    '只输出 JSON：{"terms": ["词一", "词二"]}，按重要性排序。'
)


def build_proposal_messages(
    chapter_text: str, glossary: Dict[str, Dict], *, limit: int = PROPOSAL_LIMIT
) -> Tuple[str, str]:
    """构造"让模型提名候选关键词"的 (system, user) 消息。"""
    known: List[str] = []
    for category in ("fixed_terms", "contextual_terms", "cultural_nuances"):
        known.extend(str(key) for key in (glossary.get(category) or {}) if key)
    known_text = "、".join(dict.fromkeys(known))[:800] or "（无）"

    system = (
        _PROPOSAL_SYSTEM
        + f"\n最多提名 {max(1, limit)} 个；宁缺毋滥，不要凑数。"
    )
    user = f"【已知术语】（不要重复提名）\n{known_text}\n\n【章节正文】\n{chapter_text}"
    return system, user


def _clean_term(item) -> str:
    """把模型回复里的一项规范成词条字符串。"""
    if isinstance(item, dict):
        for key in ("term", "word", "text", "name", "词", "词条"):
            value = item.get(key)
            if isinstance(value, str):
                item = value
                break
            if isinstance(value, list) and value and isinstance(value[0], str):
                item = value[0]
                break
        else:
            return ""
    if not isinstance(item, str):
        return ""
    term = item.strip().strip("「」『』“”\"'《》【】（）()[]·,，、。:：;；!！?？ \t\r\n")
    return re.sub(r"\s+", "", term)


def _extract_term_list(raw: str) -> List:
    """从模型回复里尽量抠出词条列表（JSON 优先，其次引号/列表项兜底）。"""
    text = (raw or "").strip()
    if not text:
        return []

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    data = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if data is None:
        match = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                data = None

    if isinstance(data, dict):
        for key in ("terms", "keywords", "words", "term_list", "候选词", "词条"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    if isinstance(data, list):
        return data

    # 兜底：引号里的中文词，或 "- 词 / 1. 词 / 词：" 这类列表项
    quoted = re.findall(r"[「『“\"']([\u4e00-\u9fff]{2,8})[」』”\"']", text)
    if quoted:
        return quoted
    listed: List[str] = []
    for line in text.splitlines():
        match = re.match(
            r"\s*(?:[-*·]|\d+[.、)]|[\u4e00-\u9fff]{2,8}\s*[:：])\s*([\u4e00-\u9fff]{2,8})",
            line,
        )
        if match:
            listed.append(match.group(1))
    return listed


def parse_proposals(
    raw: str,
    chapter_text: str,
    glossary: Dict[str, Dict],
    *,
    limit: int = PROPOSAL_LIMIT,
) -> List[str]:
    """把模型回复解析成候选词列表。

    硬性校验：必须**原样出现在本章正文**、2~8 字、不在 fixed_terms 里，
    否则就是模型凭空造词或复述提示词，直接丢弃。
    """
    fixed = {str(key) for key in (glossary.get("fixed_terms") or {}) if key}
    picked: List[str] = []
    seen = set()
    for item in _extract_term_list(raw):
        term = _clean_term(item)
        if not term or term in seen:
            continue
        if not (2 <= len(term) <= 8) or term in fixed:
            continue
        if term not in chapter_text:
            continue
        seen.add(term)
        picked.append(term)
        if len(picked) >= limit:
            break
    return picked


class TermResearcher:
    """把"候选词 → 检索片段"串起来的门面。"""

    def __init__(self, work_dir: str, *, scope: str = "future", window: int = 100,
                 snippets_per_term: int = 3, max_terms: int = 10, budget: int = 9000):
        self.index = CorpusIndex(work_dir)
        self.scope = scope
        self.window = window
        self.snippets_per_term = snippets_per_term
        self.max_terms = max_terms
        self.budget = budget

    def propose_terms(
        self,
        chapter_text: str,
        glossary: Dict[str, Dict],
        *,
        complete: Callable[[str, str], str],
        limit: int = PROPOSAL_LIMIT,
    ) -> List[str]:
        """让模型提名关键词。complete(system, user) 返回模型回复原文。

        本方法只负责"构造提示词 → 解析并校验"，真正的网络调用由调用方注入，
        这样 CLI / 面板 / 各脚本可以复用同一套 prompt 与过滤规则。
        """
        system, user = build_proposal_messages(chapter_text, glossary, limit=limit)
        return parse_proposals(complete(system, user), chapter_text, glossary, limit=limit)

    def research(
        self,
        chapter_text: str,
        glossary: Dict[str, Dict],
        *,
        current_chapter: int,
        extra_terms: Sequence[str] = (),
    ) -> List[TermResearch]:
        candidates = extract_candidates(
            chapter_text,
            glossary,
            self.index,
            max_terms=self.max_terms,
            current_chapter=current_chapter,
            extra_terms=extra_terms,
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
            # 自动发现/模型提名的词若检索不到任何别处上下文，就没有检索价值；
            # 人工词条（cultural/contextual）即使没片段也要保留它的说明与既定译法。
            if not snippets and candidate.source in ("discovered", "proposed"):
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

    def _render_term(self, item: TermResearch, limit: int) -> Tuple[List[str], bool]:
        """渲染单个词条，控制在 limit 字符内。

        返回 (行列表, 是否因预算而截断)。说明/既定语境优先于检索片段，
        因为它们是"人工确认过"的信息，比模型自己读片段更可靠。
        """
        attrs = [f'word="{_xml_escape(item.term)}"', f'source="{item.source}"']
        if not item.snippets and item.note:
            # 说明这类词为什么没有片段，免得模型以为检索失败
            attrs.append('note_only="true"')
        if item.total:
            attrs.append(f'occurrences="{item.total}"')
        if item.chapters:
            attrs.append(f'also_in="{",".join(str(n) for n in item.chapters[:8])}"')

        opening = f"  <term {' '.join(attrs)}>"
        closing = "  </term>"
        dropped = False
        budget_left = limit - len(opening) - len(closing) - 2

        body: List[str] = []
        if item.note:
            if budget_left < 40:
                dropped = True
            else:
                text = _clip(item.note, min(300, max(40, budget_left // 2)))
                line = f"    <localization_note>{_xml_escape(text)}</localization_note>"
                body.append(line)
                budget_left -= len(line) + 1
        for ctx_key, ctx_val in list(item.existing.items())[:4]:
            if budget_left < 60:
                dropped = True
                break
            context = _xml_escape(_clip(ctx_key, 40))
            value = _xml_escape(_clip(ctx_val, min(120, max(40, budget_left // 2))))
            line = f'    <established_context context="{context}">{value}</established_context>'
            body.append(line)
            budget_left -= len(line) + 1
        for snippet in item.snippets:
            if budget_left < 60:
                dropped = True
                break
            body_text = _clip(snippet.text, min(320, max(60, budget_left - 40)))
            line = (
                f'    <{snippet.side} chapter="{snippet.chapter}">'
                f"{_xml_escape(body_text)}</{snippet.side}>"
            )
            body.append(line)
            budget_left -= len(line) + 1

        return [opening, *body, closing], dropped

    def render(self, results: Sequence[TermResearch]) -> str:
        """渲染成注入提示词的 XML 片段；超出预算则按词条均摊截断。"""
        if not results:
            return ""

        lines: List[str] = [
            "<term_context_research>",
            "  <!-- 系统自动检索出的候选词上下文，仅供判断词义与选定译法使用。",
            "       严禁翻译或复述以下片段；严禁把后文情节、伏笔、结局提前写进译文。 -->",
        ]
        footer = "</term_context_research>"
        available = max(0, self.budget - sum(len(line) + 1 for line in lines) - len(footer) - 1)

        used = 0
        truncated = False
        for position, item in enumerate(results):
            remaining = len(results) - position
            # 均摊：前面词条省下的预算会留给后面的词条，而不是让后几个直接消失
            share = (available - used) // remaining if remaining else available
            share = max(MIN_TERM_BLOCK, min(MAX_TERM_BLOCK, share))
            block, dropped = self._render_term(item, share)
            block_chars = sum(len(line) + 1 for line in block)
            if used + block_chars > available:
                truncated = True
                break
            used += block_chars
            lines.extend(block)
            if dropped:
                truncated = True

        if truncated:
            lines.append("  <!-- 预算有限，部分片段或词条已省略 -->")
        lines.append(footer)
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
