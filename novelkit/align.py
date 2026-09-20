"""中英段落对齐（Gale-Church 长度比例算法）。

原文与译文都是"一行一段"，但译文常把 2~3 段并成一段、偶尔也拆开。按下标配对、
或只比较单段长度占比，都会在合并点之后整体错位一格。这里采用经典做法
（Gale & Church, 1993）：把 log(英文段长/中文段长) 建模为正态分布，
用 DP 在 1:1 / 1:0 / 0:1 / 2:1 / 1:2 / 2:2 / 3:1 / 1:3 中找总代价最小的分组。

面板（webpanel）与 retranslate.py 共用这一份实现，保证"段落编号"在两边含义一致。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def split_paragraphs(text: str) -> List[str]:
    """按行切段并去掉空行（原文与译文都是"一行一段"的格式）。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


# Gale-Church 对齐参数：把 log(英文长度/中文长度) 建模为正态分布，
# sigma 控制"允许多大的长度比例偏差"，gap 是整段缺失的代价，
# merge_penalty 让 DP 在代价接近时优先选择 1:1 而不是合并。
_ALIGN_SIGMA = 0.55
_ALIGN_GAP = 1.6
# 合并惩罚：太低会把本该 1:1 的短句并成一行（x:y 角标乱跳），
# 太高则会把译文本该合并的段落强行拆开。经验值取 0.6——
# 已在「90:66 大量合并」与「81:81 基本 1:1」两类极端章节上验证。
_ALIGN_MERGE_PENALTY = 0.6
_ALIGN_STEPS = ((1, 1), (1, 0), (0, 1), (2, 1), (1, 2), (2, 2), (3, 1), (1, 3))

_ALIGN_CACHE: Dict[Tuple[str, float, float], List[Dict[str, Any]]] = {}
_ALIGN_CACHE_MAX = 64
# 等量合并（2:2 / 3:3）与拆成多个 1:1 的代价完全相同，DP 会随手挑一个，
# 可能把段落边界切错一格（实测把"系统？"配到了下一行）。
# 加一点微小惩罚，让代价相同时优先拆成 1:1；远小于 merge_penalty，不会误拆真合并。
_ALIGN_EVEN_MERGE_TIEBREAK = 0.05


def align_paragraphs(zh: List[str], en: List[str]) -> List[Dict[str, Any]]:
    """把中文段落与英文段落做单调对齐（Gale-Church 长度比例算法）。

    为什么不能简单按下标配对：译文的段落边界与原文并不一一对应。以 air/1 为例，
    中文 90 段、英文 66 段——模型有时把 2~3 段中文并成一段英文，偶尔也会拆开。
    一旦发生合并，后面全部错位一格（正文看起来就是"整体串行"）。

    经典做法（Gale & Church, 1993）是假设两边长度比近似对数正态分布，
    用 DP 在 1:1 / 1:0 / 0:1 / 2:1 / 1:2 / 2:2 / 3:1 / 1:3 这些分组方式里
    找总代价最小的组合。实测能正确还原合并点，且不丢不重。

    返回的每一行形如：
        {"zh": 合并后的中文, "en": 合并后的英文,
         "zh_parts": [...], "en_parts": [...], "gap": None|"en"|"zh"}
    gap="en" 表示这一行只有中文（英文缺失），"zh" 反之。
    """
    n, m = len(zh), len(en)
    if n == 0 or m == 0:
        rows = [{"zh": t, "en": None, "zh_parts": [t], "en_parts": [], "gap": "en"} for t in zh]
        rows += [{"zh": None, "en": t, "zh_parts": [], "en_parts": [t], "gap": "zh"} for t in en]
        return rows

    len_zh = [max(1, len(x)) for x in zh]
    len_en = [max(1, len(x)) for x in en]
    total_zh = sum(len_zh)
    total_en = sum(len_en)
    mu = math.log(total_en / total_zh) if total_zh else 0.0

    def group_cost(zs: List[int], es: List[int]) -> float:
        if not zs or not es:
            return _ALIGN_GAP
        size_z, size_e = sum(zs), sum(es)
        deviation = math.log(size_e / size_z) - mu
        cost = (0.5 * (deviation / _ALIGN_SIGMA) ** 2
                + _ALIGN_MERGE_PENALTY * (len(zs) + len(es) - 2))
        if len(zs) == len(es) > 1:
            cost += _ALIGN_EVEN_MERGE_TIEBREAK
        return cost

    infinity = float("inf")
    dp = [[infinity] * (m + 1) for _ in range(n + 1)]
    back: List[List[Optional[Tuple[int, int]]]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for i in range(n + 1):
        row = dp[i]
        for j in range(m + 1):
            base = row[j]
            if base == infinity:
                continue
            for step_z, step_e in _ALIGN_STEPS:
                ni, nj = i + step_z, j + step_e
                if ni > n or nj > m:
                    continue
                cost = base + group_cost(len_zh[i:ni], len_en[j:nj])
                if cost < dp[ni][nj]:
                    dp[ni][nj] = cost
                    back[ni][nj] = (i, j)

    groups: List[Tuple[Tuple[int, ...], Tuple[int, ...]]] = []
    i, j = n, m
    while i or j:
        previous = back[i][j]
        if previous is None:  # 理论上不可达，防御性兜底
            previous = (max(0, i - 1), max(0, j - 1))
        pi, pj = previous
        groups.append((tuple(range(pi, i)), tuple(range(pj, j))))
        i, j = pi, pj
    groups.reverse()

    rows: List[Dict[str, Any]] = []
    for zi, ei in groups:
        zh_parts = [zh[k] for k in zi]
        en_parts = [en[k] for k in ei]
        gap = None if (zh_parts and en_parts) else ("en" if zh_parts else "zh")
        rows.append({
            "zh": "\n".join(zh_parts) if zh_parts else None,
            "en": "\n".join(en_parts) if en_parts else None,
            "zh_parts": zh_parts,
            "en_parts": en_parts,
            "gap": gap,
        })
    return rows


def align_cached(zh_path: Path, en_path: Path, zh: List[str], en: List[str]) -> List[Dict[str, Any]]:
    """按 (路径, mtime) 缓存对齐结果——同一章反复查看时不必重算。"""
    def stamp(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    key = (str(zh_path), stamp(zh_path), stamp(en_path))
    cached = _ALIGN_CACHE.get(key)
    if cached is not None:
        return cached

    rows = align_paragraphs(zh, en)
    if len(_ALIGN_CACHE) >= _ALIGN_CACHE_MAX:
        _ALIGN_CACHE.pop(next(iter(_ALIGN_CACHE)))
    _ALIGN_CACHE[key] = rows
    return rows
