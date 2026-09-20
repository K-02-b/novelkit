#!/usr/bin/env python3
"""局部重译：只重译指定段落，其余正文原样保留。

用法：
    python retranslate.py --dir two --chapter 41 --rows 3,7-9
    python retranslate.py --dir two --chapter 41 --rows 3 --rag --model deepseek-v3.2

设计要点
--------
* **段落编号来自 Gale-Church 对齐结果**（novelkit.align），与面板里看到的行号一致。
  一个对齐行可能包含多段中文，选中其中任意一段即整行重译——否则无法把那一段
  对应的英文从合并段里拆出来。
* **上下文 / 提示词 / 术语库 / RAG 与整章翻译完全一致**：都走 novelkit.prompt，
  不另写一套，避免局部重译的语气与术语跟全文脱节。
* 只替换选中行的英文段落，其它行**逐字节保留**；写回用原子写入。
* 新提取的术语照常走"先到先得 + 冲突拦截"（globals 优先），与整章翻译同规则。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelkit import align as nkalign  # noqa: E402
from novelkit import config, llm  # noqa: E402
from novelkit import glossary as nkglossary  # noqa: E402
from novelkit import prompt as nkprompt  # noqa: E402
from novelkit import rag as nkrag  # noqa: E402
from novelkit import text as nktext  # noqa: E402
from novelkit import ui  # noqa: E402

GLOBAL_GLOSSARY = config.GLOBAL_GLOSSARY_PATH
DEFAULT_PROMPT = config.DEFAULT_PROMPT_PATH

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
EXIT_DEPENDENCY = 3


def parse_rows(spec: str) -> List[int]:
    """解析 '3,7-9' 形式的行号（1 基，与面板显示一致）。"""
    rows: List[int] = []
    for token in (spec or "").replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = (int(x) for x in token.split("-", 1))
            if end < start:          # "9-7" 视为 7-9，而不是静默返回空
                start, end = end, start
            rows.extend(range(start, end + 1))
        else:
            rows.append(int(token))
    return sorted({r for r in rows if r > 0})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="局部重译指定段落（上下文/术语库/RAG 与整章翻译一致）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dir", required=True, metavar="PATH",
                        help="作品目录（相对路径优先在 works/ 工作区里查找）")
    parser.add_argument("--chapter", type=int, required=True, help="章节号")
    parser.add_argument("--rows", required=True, metavar="SPEC", help="要重译的行号，如 '3,7-9'（1 基）")
    parser.add_argument("--model", default=config.get_model(), help="模型 ID")
    parser.add_argument("--base_url", default=config.get_base_url(), help="API 地址")
    parser.add_argument("--api_key", default=None, help="API Key（默认读 .env）")
    parser.add_argument("--rag", action="store_true", help="启用 RAG 术语上下文检索")
    parser.add_argument("--context", type=int, default=500, help="参考前文长度（负数=章节数）")
    parser.add_argument("--future", type=int, default=0, help="参考后文长度（负数=章节数）")
    parser.add_argument("--anchor", type=int, choices=[0, 1, 2], default=1, help="风格基准强度")
    parser.add_argument("--temp", type=float, default=0.7)
    parser.add_argument("--top_p", type=float, default=0.8)
    parser.add_argument("--presence_penalty", type=float, default=0.3)
    parser.add_argument("--repetition_penalty", type=float, default=1.1)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--thinking_budget", type=int, default=8192)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--allow-term-changes", action="store_true",
                        help="允许覆盖章节级既定译法（全局 glossary.json 永不可覆盖）")
    parser.add_argument("--instruction", default="", metavar="TEXT",
                        help="本次任务的补充要求，会以 <user_supplement> 注入提示词")
    parser.add_argument("--instruction-file", default=None, metavar="PATH",
                        help="从文件读取补充要求（面板用这种方式传长文本）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发送的内容，不调用 API、不写文件")
    return parser


# ---------------------------------------------------------------------------
# 上下文（与 main.py 同规则：正数=长度，负数=章节数）
# ---------------------------------------------------------------------------

def anchor_text(work_dir: Path, target: int, strength: int) -> str:
    parts: List[str] = []
    if strength >= 1:
        intro = nktext.read_text(str(work_dir / "0_translated.txt"), "")
        if intro and intro.strip():
            parts.append(f"<work_introduction>\n{intro.strip()}\n</work_introduction>")
    if strength == 2 and target > 1:
        first = nktext.read_text(str(work_dir / "1_translated.txt"), "")
        if first and first.strip():
            words = first.strip().split()
            parts.append(f"<first_chapter_sample>\n{' '.join(words[:500])}\n</first_chapter_sample>")
    return "\n".join(parts)


def previous_context(work_dir: Path, target: int, param: int) -> str:
    if param == 0 or target <= 1:
        return ""
    if param > 0:
        remaining, chunks = param, []
        for index in range(target - 1, 0, -1):
            text = nktext.read_text(str(work_dir / f"{index}_translated.txt"), "")
            if not text or not text.strip():
                continue
            size = nktext.count_units(text, "words")
            if size <= remaining:
                chunks.insert(0, text.strip())
                remaining -= size
            else:
                chunks.insert(0, nktext.take_tail(text, remaining, "words"))
                remaining = 0
            if remaining <= 0:
                break
        return "\n\n".join(chunks)

    needed, chunks = abs(param), []
    for index in range(target - 1, 0, -1):
        text = nktext.read_text(str(work_dir / f"{index}_translated.txt"), "")
        if text and text.strip():
            chunks.insert(0, text.strip())
            needed -= 1
        if needed <= 0:
            break
    return "\n\n".join(chunks)


def future_context(work_dir: Path, target: int, param: int) -> str:
    if param == 0:
        return ""
    chunks: List[str] = []
    index = target + 1
    if param > 0:
        remaining = param
        while remaining > 0:
            raw = nktext.read_text(str(work_dir / f"{index}_origin.txt"))
            if raw is None:
                break
            text = raw.strip()
            if not text:
                index += 1
                continue
            size = nktext.count_units(text, "chars")
            if size <= remaining:
                chunks.append(text)
                remaining -= size
            else:
                chunks.append(nktext.take_head(text, remaining, "chars"))
                remaining = 0
            index += 1
        return "\n\n".join(chunks)

    needed = abs(param)
    while needed > 0:
        raw = nktext.read_text(str(work_dir / f"{index}_origin.txt"))
        if raw is None:
            break
        if raw.strip():
            chunks.append(raw.strip())
            needed -= 1
        index += 1
    return "\n\n".join(chunks)



def merge_translations(
    rows: List[Dict[str, Any]],
    en_paras: List[str],
    selected: List[Any],
    by_row: Dict[int, str],
) -> Any:
    """把模型返回的段落译文按行回填，返回 (新的英文段落列表, 替换段数)。

    只动选中行覆盖的那些英文段落，其余段落**原样保留**（这是"局部重译"的核心承诺）。
    一个对齐行可能对应多段英文（1 中文 : N 英文），此时用一段新译文整体替换；
    该行原本没有英文时，补译插到前一行译文之后。

    所有行的落点必须**先按原始 en_paras 一次性算好**再回填。若边改边算，前面某行把
    1 段英文重译成 2 段（很常见：中文两段挤在一行、英文漏译了一段）之后，后面选中行
    的下标会整体后移一位，于是它的区间正好盖住刚补回来的那段，把它覆盖掉——
    用户勾选两段重译、结果"系统？"消失就是这么来的。
    """
    # 第 1 步：解析每行的落点。
    #   span：命中现有英文 [start, end]，整段替换；
    #   gap ：该行还没有英文，补译排在前一行译文之后。落点用"最小下标"记录
    #        （gap_floor），实际插入位置 = max(该下标, 游标)，这样 anchor 行自己
    #        是 gap 时也能排到它后面。
    plan: Dict[int, Tuple[str, Optional[int], Optional[int], Optional[int]]] = {}
    for number, row in selected:
        indices = [i for i, part in enumerate(en_paras) if part in (row.get("en_parts") or [])]
        if indices:
            plan[number] = ("span", min(indices), max(indices), None)
            continue
        gap_floor: Optional[int] = None
        for previous in reversed(range(number - 1)):
            if previous + 1 in plan:               # 前一行也在本次处理里
                gap_floor = plan[previous + 1][1]   # 它的 start；gap 行则两者都 None
                if gap_floor is None:
                    gap_floor = 0                  # 紧贴前一行，由游标决定
                break
            found = [i for i, part in enumerate(en_paras)
                     if part in (rows[previous].get("en_parts") or [])]
            if found:                              # 前一行是普通行：贴到它英文之后
                gap_floor = max(found) + 1
                break
        plan[number] = ("gap", gap_floor, None, None)

    # 第 2 步：按行号顺序单向扫描输出，游标只前进。
    #   span 行：补上它之前的未选中段落，再写入新译文，并消费掉 [start, end]；
    #   gap 行 ：先补上落到它之前的所有旧段落，再插入补译（不消费旧段落，
    #            因此它后面的 span 行仍从自己原来的下标继续）。
    new_en: List[str] = []
    cursor = 0
    replaced = 0
    for number, row in selected:
        text = by_row.get(number)
        if not text:
            continue
        paragraphs = [nktext.normalize_punctuation(line).strip()
                      for line in text.splitlines() if line.strip()]
        if not paragraphs:
            continue
        kind, start, end, _ = plan[number]
        replaced += 1

        if kind == "gap":
            position = max(cursor, start or 0)
            new_en.extend(en_paras[cursor:position])   # 未选中段落逐字节保留
            new_en.extend(paragraphs)                  # 补译插在前一行译文之后
            cursor = position
            continue

        if start is None:
            new_en.extend(paragraphs)
            continue
        new_en.extend(en_paras[cursor:start])          # 未选中段落逐字节保留
        new_en.extend(paragraphs)
        cursor = end + 1

    new_en.extend(en_paras[cursor:])
    return new_en, replaced


# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = config.resolve_work_dir(args.dir)
    if not work_dir.is_dir():
        ui.error(f"工作目录不存在: {work_dir}")
        return EXIT_FATAL

    try:
        wanted = parse_rows(args.rows)
    except ValueError as exc:
        ui.error(f"--rows 解析失败: {exc}")
        return EXIT_FATAL
    if not wanted:
        ui.error("未指定任何行号")
        return EXIT_FATAL

    zh_path = work_dir / f"{args.chapter}_origin.txt"
    en_path = work_dir / f"{args.chapter}_translated.txt"
    if not zh_path.exists():
        ui.error(f"找不到原文: {zh_path}")
        return EXIT_FATAL
    if not en_path.exists():
        ui.error(f"该章尚未翻译，无法局部重译: {en_path}")
        return EXIT_FATAL

    zh_paras = nkalign.split_paragraphs(nktext.read_text(str(zh_path), ""))
    en_paras = nkalign.split_paragraphs(nktext.read_text(str(en_path), ""))
    if not zh_paras or not en_paras:
        ui.error("原文或译文为空，无法对齐")
        return EXIT_FATAL

    rows = nkalign.align_paragraphs(zh_paras, en_paras)
    ui.info(f"对齐完成：{len(rows)} 行（中文 {len(zh_paras)} 段 / 英文 {len(en_paras)} 段）")

    invalid = [r for r in wanted if r > len(rows)]
    if invalid:
        ui.error(f"行号超出范围（本章共 {len(rows)} 行）: {invalid}")
        return EXIT_FATAL

    selected = [(number, rows[number - 1]) for number in wanted]
    empty = [num for num, row in selected if not row["zh_parts"]]
    if empty:
        ui.warn(f"以下行没有中文原文（仅英文），已跳过: {empty}")
        selected = [(num, row) for num, row in selected if row["zh_parts"]]
    if not selected:
        ui.error("选中的行都没有中文原文，无法重译")
        return EXIT_FATAL

    segments = [{"row": number, "text": "\n".join(row["zh_parts"])} for number, row in selected]
    ui.info("待重译段落: " + "、".join(f"第 {s['row']} 行（{len(s['text'])} 字）" for s in segments[:8])
            + ("…" if len(segments) > 8 else ""))

    # ---- 术语库（全局优先 + 先到先得，与 main.py 同一套） ----
    # 局部重译的是**本章已有译文**，所以要把本章 glossary_<N>.json 也算进来：
    # main.py 用 merged_up_to(N) 是因为第 N 章还没翻译（它的术语库正在生成），
    # 而重译时必须沿用本章已经定下的译法，否则会各译各的。
    store = nkglossary.GlossaryStore(str(work_dir), str(GLOBAL_GLOSSARY))
    combined = store.merged_up_to(args.chapter + 1)
    chapter_text = "\n".join(zh_paras)
    future_ctx = future_context(work_dir, args.chapter, args.future)
    relevant = store.filter_for_content(combined, chapter_text, future_ctx)

    system_prompt = nkprompt.load_system_prompt(DEFAULT_PROMPT, work_dir)
    anchor = anchor_text(work_dir, args.chapter, args.anchor)
    prev_ctx = previous_context(work_dir, args.chapter, args.context)

    # ---- RAG（与整章翻译同一实现） ----
    research = ""
    if args.rag:
        try:
            researcher = nkrag.TermResearcher(str(work_dir))
            findings = researcher.research(chapter_text, relevant, current_chapter=args.chapter)
            research = researcher.render(findings)
            if findings:
                ui.info("RAG 检索到 " + "、".join(f"{f.term}({len(f.snippets)})" for f in findings))
        except Exception as exc:  # noqa: BLE001
            ui.warn(f"RAG 检索失败，已跳过: {exc}")

    instruction = ""
    if args.instruction_file:
        if os.path.exists(args.instruction_file):
            instruction = nktext.read_text(args.instruction_file, "") or ""
            ui.info(f"已载入补充要求（{len(instruction.strip())} 字符）")
        else:
            ui.warn(f"补充要求文件不存在，已忽略: {args.instruction_file}")
    else:
        instruction = args.instruction or ""

    system, user = nkprompt.build_partial_messages(
        system_prompt, segments, relevant,
        anchor=anchor, prev_context=prev_ctx, future_context=future_ctx,
        research=research, extra_instruction=instruction,
    )

    if args.dry_run:
        ui.info(f"[dry-run] system {len(system)} 字符 / user {len(user)} 字符")
        print(user[:2000])
        return EXIT_OK

    # ---- 调用模型 ----
    try:
        client = llm.create_client(api_key=args.api_key, base_url=args.base_url, timeout=args.timeout)
    except llm.DependencyError as exc:
        ui.error(str(exc))
        return EXIT_DEPENDENCY
    except Exception as exc:  # noqa: BLE001
        ui.error(str(exc))
        return EXIT_FATAL

    params: Dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": args.temp,
        "top_p": args.top_p,
        "presence_penalty": args.presence_penalty,
        "extra_body": {
            "repetition_penalty": args.repetition_penalty,
            "enable_thinking": bool(args.thinking),
            "thinking_budget": args.thinking_budget,
        },
    }

    def attempt() -> Dict[str, Any]:
        response = llm.call_chat(client, retries=0, **params)
        message = response.choices[0].message
        data = llm.extract_json(message.content or "")
        payload = llm.coerce_translation_payload(data)
        translations = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(translations, list) or not translations:
            raise llm.ResponseParseError("返回结果缺少 translations 数组")
        payload["translations"] = translations
        return payload

    try:
        result = llm.run_with_retry(
            attempt, retries=args.retries, base_delay=args.retry_delay,
            on_retry=lambda n, exc, delay: ui.warn(
                f"第 {n} 次重试（{delay:.1f}s 后）：{str(exc).splitlines()[0][:120]}"),
        )
    except Exception as exc:  # noqa: BLE001
        ui.error(f"重译失败: {exc}")
        return EXIT_PARTIAL

    # ---- 按编号回填 ----
    by_row: Dict[int, str] = {}
    for item in result["translations"]:
        if not isinstance(item, dict):
            continue
        try:
            row_number = int(item.get("row"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            by_row[row_number] = text

    missing = [number for number, _ in selected if number not in by_row]
    if missing:
        ui.warn(f"模型未返回以下行的译文，这些行保持原样: {missing}")

    new_en, replaced = merge_translations(rows, en_paras, selected, by_row)
    if not replaced:
        ui.error("没有任何段落被替换，未写入文件")
        return EXIT_PARTIAL

    new_text = "\n\n".join(new_en) + "\n"
    # 重译第 1 行时同样要守住"首行 = Chapter NN: 标题"的统一格式
    heading_problem = nktext.check_chapter_heading(
        nktext.read_text(str(zh_path), "") or "", new_text)
    if heading_problem:
        ui.warn(f"第 {args.chapter} 章 {heading_problem}")

    nktext.write_text_atomic(str(en_path), new_text)
    ui.ok(f"已重译并写回 {replaced} 段 -> {en_path.name}")

    # ---- 术语入库（与整章翻译同规则：先到先得 + 冲突拦截） ----
    proposed = nkglossary.build_chapter_glossary(result)
    if any(proposed.values()):
        clean, conflicts = store.sanitize_chapter(
            args.chapter, proposed, allow_changes=args.allow_term_changes)
        if any(clean.values()):
            store.save_chapter(args.chapter, clean)
        tracker_exists = (work_dir / "global_glossary_tracker.json").exists()
        if tracker_exists or any(clean.values()):
            store.write_tracker()
        store.record_conflicts(conflicts)
        blocked = [c for c in conflicts if c["action"] == "ignored"]
        if blocked:
            ui.warn(f"拦截 {len(blocked)} 条重复术语: "
                    + "、".join(f"{c['term']}→{c['old']}" for c in blocked[:4]))
        kept = sum(len(v) for v in clean.values())
        ui.info(f"本章术语库更新：新增 {kept} 条")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
