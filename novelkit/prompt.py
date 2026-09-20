"""提示词组装：scripts/translate.py（整章翻译）与 scripts/retranslate.py（局部重译）共用。

把这段逻辑抽出来的唯一目的是**保证两边注入的东西完全一致**：
同一份 system prompt、同一套术语库、同样的前文/后文上下文、同样的 RAG 检索块。
如果各写一份，局部重译迟早会漏掉某个上下文，产出的语气/术语就会和整章翻译不一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# RAG 检索结果的使用说明（仅在真的带上了检索资料时附加）
RAG_INSTRUCTION = (
    "【术语检索资料使用规则】\n"
    "如果输入中提供了 <term_context_research>，那是系统从全书（含后文）自动检索出的"
    "候选词上下文，用途只有一个：帮助你判断该词的确切含义、从而选定合适译法。\n"
    "- 严禁翻译、复述或引用这些片段；严禁把后文的情节、伏笔、结局提前写进译文。\n"
    "- 若词条带 fixed_translation 或 established_context，说明该词已有既定译法，必须沿用。\n"
    "- 若词条带 localization_note，那是人工整理的文化/俚语本地化建议，优先采纳。\n"
    "- 检索结果仅供参考；若与当前章节语境冲突，以当前章节为准（既定译法除外）。"
)


def load_system_prompt(prompt_path, work_dir: Optional[Path] = None) -> str:
    """全局提示词（config/prompt.txt）+ 作品级附加提示词（<作品>/prompt.txt）。

    work_dir 下若存在旧文件名 `提示词.txt` 也会读，方便旧作品平滑迁移。
    """
    global_path = Path(prompt_path)
    if not global_path.exists():
        raise FileNotFoundError(f"未找到全局提示词文件: {global_path}")

    prompt = global_path.read_text(encoding="utf-8").strip()
    if work_dir:
        local = _read_work_prompt(Path(work_dir))
        if local:
            prompt += f"\n\n【附加提示词】\n{local}"
    return prompt


def _read_work_prompt(work_dir: Path) -> str:
    """作品级附加提示词：优先 prompt.txt，兼容旧的 提示词.txt。"""
    for name in ("prompt.txt", "提示词.txt"):
        path = work_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def build_messages(
    system_prompt: str,
    content: str,
    relevant_glossary: Dict[str, Any],
    *,
    anchor: str = "",
    prev_context: str = "",
    future_context: str = "",
    draft: Optional[str] = None,
    feedback: Optional[str] = None,
    research: str = "",
    extra_instruction: str = "",
) -> Tuple[str, str]:
    """构造 (system, user)。

    content 既可以是整章原文，也可以是"若干编号段落"的局部重译输入——
    区别只体现在 extra_instruction 与 content 的写法上，其余注入完全一致。
    """
    system = f"{system_prompt}\n\n【术语库】\n{json.dumps(relevant_glossary, ensure_ascii=False, indent=2)}"
    if research:
        system += f"\n\n{RAG_INSTRUCTION}"

    user = ""
    if anchor:
        user += f"<style_baseline>\n{anchor}\n</style_baseline>\n\n"

    if prev_context or future_context:
        user += "<context_reference>\n"
        if prev_context:
            user += f"  <previous>\n{prev_context}\n  </previous>\n"
        if future_context:
            user += f"  <future>\n{future_context}\n  </future>\n"
        user += "</context_reference>\n\n"

    if research:
        user += research + "\n\n"

    user += f"<source_to_translate>\n{content}\n</source_to_translate>"

    if draft and feedback:
        user += f"\n\n<draft_translation>\n{draft}\n</draft_translation>\n"
        user += f"\n<review_feedback>\n{feedback}\n</review_feedback>\n"
        user += "\n请参考上述初稿和评审意见，重新翻译源文本，提供最终改进版本。\n"

    if extra_instruction and extra_instruction.strip():
        # 使用者的临时补充要求单独包一层标签，便于提示词里声明它的优先级与边界
        user += ("\n\n<user_supplement>\n"
                 f"{extra_instruction.strip()}\n"
                 "</user_supplement>")

    return system, user


def build_partial_messages(
    system_prompt: str,
    segments: List[Dict[str, Any]],
    relevant_glossary: Dict[str, Any],
    *,
    anchor: str = "",
    prev_context: str = "",
    future_context: str = "",
    research: str = "",
    extra_instruction: str = "",
) -> Tuple[str, str]:
    """局部重译的输入：只把选中的段落编号后放进 <source_to_translate>。

    上下文、术语库、RAG 检索块与整章翻译完全一致；额外要求模型按编号回填，
    并明确"未选中的段落不要重写"，避免它顺手改动别的段落。
    """
    lines = []
    for segment in segments:
        lines.append(f"[{segment['row']}] {segment['text']}")
    content = "\n".join(lines)

    instruction = (
        "【局部重译要求】\n"
        "上面 <source_to_translate> 里是**本章的部分段落**，每段前面方括号内的数字是段落编号。\n"
        "- 只翻译这些段落，并且**严格按编号返回**，不要合并、拆分或遗漏。\n"
        "- 未列出的段落不要出现在结果里。\n"
        "- 译文必须与前文、后文以及术语库保持连贯一致，语气、时态、人称与已发布章节统一。\n"
        "- 输出的 JSON 必须包含全部键：translated_content、new_fixed_terms、"
        "new_contextual_terms、new_aesthetic_sentences、new_cultural_nuances；"
        "没有内容时用空字符串或空对象 {}，**不得省略任何键**。\n"
        "- translations 数组中每个元素形如 {\"row\": 编号, \"text\": \"该段英文译文\"}。"
    )

    system, user = build_messages(
        system_prompt, content, relevant_glossary,
        anchor=anchor, prev_context=prev_context, future_context=future_context,
        research=research, extra_instruction=extra_instruction,
    )
    # 局部重译需要额外字段承载"按编号回填"的结果
    system += (
        "\n\n【局部重译输出格式】\n"
        "除了常规字段外，必须再返回 translations 数组：\n"
        '{\n  "translations": [ { "row": 1, "text": "英文译文" } ],\n'
        '  "new_fixed_terms": { "中文": "英文" },\n'
        '  "new_contextual_terms": { "中文": { "语境": "英文" } }\n}\n'
        "translated_content 可以留空字符串，但**键必须存在**。"
    )
    return system, user + "\n\n" + instruction


def build_refine_messages(
    system_prompt: str,
    segments: List[Dict[str, Any]],
    relevant_glossary: Dict[str, Any],
    *,
    anchor: str = "",
    prev_context: str = "",
    future_context: str = "",
    research: str = "",
    extra_instruction: str = "",
    ignore_draft: bool = False,
) -> Tuple[str, str]:
    """refine 的输入：给出原文段落与**现有译文**，要求逐段点评并给出修订稿与理由。

    segments 的每一项：{"row": 行号, "zh": 中文, "en": 现有英文}。
    上下文 / 术语库 / RAG / 补充要求与整章翻译一致，避免修订后的文风脱节。

    ignore_draft=True 时不把现有译文给模型（等价于重新翻译，但仍要求给出理由）——
    用于现译已经错得离谱、怕被它带偏的段落。
    """
    lines = []
    for segment in segments:
        lines.append(f"[{segment['row']}]")
        lines.append(f"  原文: {segment['zh']}")
        if not ignore_draft:
            lines.append(f"  现译: {segment.get('en') or '（空）'}")
    content = "\n".join(lines)

    if ignore_draft:
        instruction = (
            "【本段任务：重新翻译并说明取舍】\n"
            "上面 <source_to_translate> 里是**本章的部分段落**，每段给出行号与中文原文"
            "（**刻意不提供现有译文**，避免被它带偏）。\n"
            "请对这些段落逐一做两件事：\n"
            "1. 给出你认为最好的英文译文；\n"
            "2. 用一句话说明**翻译时的取舍或处理理由**（中文即可），便于人工复核。\n"
            "要求：\n"
            "- 严格按行号返回，不要合并、拆分或遗漏；只处理给出的行。\n"
            "- 术语库中的既定译法必须沿用。\n"
            "- 数字、单位、专有名词与标点结构保持准确。"
        )
    else:
        instruction = (
            "【本段任务：逐段评审并修订】\n"
            "上面 <source_to_translate> 里是**本章的部分段落**，每段给出行号、中文原文与现有英文译文。\n"
            "请对这些段落逐一做三件事：\n"
            "1. 指出当前译文的问题（漏译、误译、语气/时态不一致、术语不符、英文不地道等）；\n"
            "2. 给出修订后的英文译文；\n"
            "3. 用一句话说明**这次改动的理由**（中文即可），便于人工复核。\n"
            "要求：\n"
            "- 严格按行号返回，不要合并、拆分或遗漏；只处理给出的行。\n"
            "- 术语库中的既定译法必须沿用，不得借修订之名替换。\n"
            "- 数字、单位、专有名词与标点结构保持准确。\n"
            "- 若某段现译已经足够好，可以原样保留，但仍要说明「无需修改」的理由。"
        )

    system, user = build_messages(
        system_prompt, content, relevant_glossary,
        anchor=anchor, prev_context=prev_context, future_context=future_context,
        research=research, extra_instruction=extra_instruction,
    )
    system += (
        "\n\n【修订输出格式】\n"
        "除了常规字段外，必须再返回 revisions 数组：\n"
        '{\n  "revisions": [ { "row": 1, "text": "修订后的英文译文", "reason": "改动理由（中文）" } ],\n'
        '  "new_fixed_terms": { "中文": "英文" },\n'
        '  "new_contextual_terms": { "中文": { "语境": "英文" } }\n}\n'
        "translated_content 可以留空字符串，但**键必须存在**。"
    )
    return system, user + "\n\n" + instruction
