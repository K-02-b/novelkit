#!/usr/bin/env python3
"""Refine：让 AI 逐段点评并修订已有译文，并给出改动理由。

用法：
    python refine.py --dir two --chapter 41                 # 全章
    python refine.py --dir two --chapter 41 --rows 3,7-9    # 只修订选中的行
    python refine.py --dir two --chapter 41 --rows 3 --rag --instruction-file /tmp/instr.txt

与 retranslate.py 的区别
------------------------
* retranslate 是"**重新翻译**"：把中文当输入，产出全新译文。
* refine 是"**评审后修订**"：把"中文 + 现有译文"一起给模型，让它先找问题、
  再给修订稿，并且**必须说明改动理由**；理由会写进 `<章节>_refine.json`，
  面板在对应段落角上显示一个注释图标，悬停即可看到。

上下文 / 提示词 / 术语库 / RAG / 补充要求全部复用 novelkit.prompt，
与整章翻译、局部重译保持同一套注入。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
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

MAX_SEGMENTS = 40   # 一次修订太多段落容易顾此失彼，超出则分批

# 模型给行号/正文用的键名并不统一，这里都认
_ROW_KEYS = ("row", "index", "id", "no", "seq", "行号", "段号", "paragraph", "line")
_TEXT_KEYS = ("text", "translation", "revised", "revision", "译文", "修订")
_REASON_KEYS = ("reason", "why", "comment", "理由", "说明")


def _pick(item: Dict[str, Any], keys) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def parse_revision(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把模型返回的一条修订规范化为 {row, text, reason}；取不到行号则返回 None。"""
    if not isinstance(item, dict):
        return None
    raw_row = _pick(item, _ROW_KEYS)
    try:
        row = int(str(raw_row).strip())
    except (TypeError, ValueError):
        return None
    text = _pick(item, _TEXT_KEYS)
    reason = _pick(item, _REASON_KEYS)
    return {
        "row": row,
        "text": str(text).strip() if isinstance(text, str) else "",
        "reason": str(reason).strip() if isinstance(reason, str) else "",
    }


def parse_rows(spec: str) -> List[int]:
    rows: List[int] = []
    for token in (spec or "").replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = (int(x) for x in token.split("-", 1))
            if end < start:
                start, end = end, start
            rows.extend(range(start, end + 1))
        else:
            rows.append(int(token))
    return sorted({r for r in rows if r > 0})


def notes_path(work_dir: Path, chapter: int) -> Path:
    return work_dir / f"{chapter}_refine.json"


def text_hash(text: str) -> str:
    return hashlib.sha1(nktext.normalize_punctuation(text or "").strip().encode("utf-8")).hexdigest()[:16]


def load_notes(work_dir: Path, chapter: int) -> Dict[str, Any]:
    path = notes_path(work_dir, chapter)
    if not path.exists():
        return {"chapter": chapter, "updated": "", "notes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("notes"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"chapter": chapter, "updated": "", "notes": []}


def merge_notes(existing: Dict[str, Any], new_notes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按行号合并批注：同行的新批注覆盖旧的，其余保留。"""
    by_row: Dict[int, Dict[str, Any]] = {}
    for note in existing.get("notes", []):
        try:
            by_row[int(note.get("row"))] = note
        except (TypeError, ValueError):
            continue
    for note in new_notes:
        by_row[int(note["row"])] = note
    return {
        "chapter": existing.get("chapter"),
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": [by_row[row] for row in sorted(by_row)],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refine：逐段点评并修订译文，附改动理由",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dir", required=True, metavar="PATH",
                        help="作品目录（相对路径优先在 works/ 工作区里查找）")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--rows", default="", metavar="SPEC",
                        help="要修订的行号，如 '3,7-9'（1 基）；留空表示全章")
    parser.add_argument("--model", default=config.get_model())
    parser.add_argument("--base_url", default=config.get_base_url())
    parser.add_argument("--api_key", default=None)
    parser.add_argument("--rag", action="store_true")
    parser.add_argument("--no-rag-ask", dest="rag_ask", action="store_false",
                        help="RAG 开启时默认会额外问一次模型「哪些词要查」；加此项只跑启发式候选")
    parser.add_argument("--context", type=int, default=500)
    parser.add_argument("--future", type=int, default=0)
    parser.add_argument("--anchor", type=int, choices=[0, 1, 2], default=1)
    parser.add_argument("--temp", type=float, default=0.5)
    parser.add_argument("--top_p", type=float, default=0.8)
    parser.add_argument("--presence_penalty", type=float, default=0.3)
    parser.add_argument("--repetition_penalty", type=float, default=1.1)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--thinking_budget", type=int, default=8192)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--instruction", default="", metavar="TEXT")
    parser.add_argument("--instruction-file", default=None, metavar="PATH")
    parser.add_argument("--allow-term-changes", action="store_true")
    parser.add_argument("--ignore-draft", action="store_true",
                        help="不把现有译文交给模型（等价于重新翻译，但仍会给出理由）")
    parser.add_argument("--dry-run", action="store_true")
    return parser


# 与 main.py / retranslate.py 同规则
def _anchor_text(work_dir: Path, target: int, strength: int) -> str:
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


def _previous(work_dir: Path, target: int, param: int) -> str:
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


def _future(work_dir: Path, target: int, param: int) -> str:
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


def segment_plan(rows: List[Dict[str, Any]], wanted: List[int]) -> List[Dict[str, Any]]:
    """把选中的对齐行整理成 refine 输入；只保留"有原文也有译文"的行。"""
    plan: List[Dict[str, Any]] = []
    for number in wanted:
        row = rows[number - 1]
        zh = "\n".join(row.get("zh_parts") or [])
        en = "\n".join(row.get("en_parts") or [])
        if not zh.strip():
            continue
        plan.append({"row": number, "zh": zh, "en": en, "en_parts": row.get("en_parts") or []})
    return plan


def apply_revisions(en_paras: List[str], rows: List[Dict[str, Any]],
                    plan: List[Dict[str, Any]], revisions: Dict[int, Dict[str, str]]):
    """把修订稿回填到英文段落里。返回 (新段落, 批注列表, 替换数)。"""
    new_en = list(en_paras)
    notes: List[Dict[str, Any]] = []
    changed = 0

    for item in plan:
        number = item["row"]
        revision = revisions.get(number)
        if not revision:
            continue
        raw = revision.get("text") or ""
        # 文件格式是"一段一行、段间空行"，所以模型返回的多行文本必须拆成真正的多段；
        # 否则把带 \n\n 的字符串当成一段写进去，会悄悄改变段落结构、连带打乱对齐。
        paragraphs = [nktext.normalize_punctuation(line).strip()
                      for line in raw.splitlines() if line.strip()]
        if not paragraphs:
            continue
        text = "\n\n".join(paragraphs)
        reason = (revision.get("reason") or "").strip()
        before = item["en"]

        indices = [i for i, part in enumerate(new_en) if part in item["en_parts"]]
        if indices:
            first, last = min(indices), max(indices)
            new_en[first:last + 1] = paragraphs
        else:
            position = 0
            for previous in reversed(range(number - 1)):
                found = [i for i, part in enumerate(new_en) if part in (rows[previous].get("en_parts") or [])]
                if found:
                    position = max(found) + 1
                    break
            for offset, paragraph in enumerate(paragraphs):
                new_en.insert(position + offset, paragraph)

        if text != before:
            changed += 1
        notes.append({
            "row": number,
            "reason": reason or "（模型未给出理由）",
            "before": before,
            "after": text,
            "changed": text != before,
            "hash": text_hash(text),
        })
    return new_en, notes, changed


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = config.resolve_work_dir(args.dir)
    if not work_dir.is_dir():
        ui.error(f"工作目录不存在: {work_dir}")
        return EXIT_FATAL

    zh_path = work_dir / f"{args.chapter}_origin.txt"
    en_path = work_dir / f"{args.chapter}_translated.txt"
    if not zh_path.exists() or not en_path.exists():
        ui.error(f"需要原文与译文都存在才能 refine：{zh_path.name} / {en_path.name}")
        return EXIT_FATAL

    zh_paras = nkalign.split_paragraphs(nktext.read_text(str(zh_path), ""))
    en_paras = nkalign.split_paragraphs(nktext.read_text(str(en_path), ""))
    if not zh_paras or not en_paras:
        ui.error("原文或译文为空，无法 refine")
        return EXIT_FATAL

    rows = nkalign.align_paragraphs(zh_paras, en_paras)
    ui.info(f"对齐完成：{len(rows)} 行（中文 {len(zh_paras)} 段 / 英文 {len(en_paras)} 段）")

    if args.rows.strip():
        try:
            wanted = parse_rows(args.rows)
        except ValueError as exc:
            ui.error(f"--rows 解析失败: {exc}")
            return EXIT_FATAL
        invalid = [r for r in wanted if r > len(rows)]
        if invalid:
            ui.error(f"行号超出范围（本章共 {len(rows)} 行）: {invalid}")
            return EXIT_FATAL
    else:
        wanted = list(range(1, len(rows) + 1))

    plan = segment_plan(rows, wanted)
    if not plan:
        ui.error("选中的行没有可修订的内容")
        return EXIT_FATAL

    store = nkglossary.GlossaryStore(str(work_dir), str(GLOBAL_GLOSSARY))
    # 精修是"就地改写"，所以**不注入本章自己确立的术语**：
    # 本章 glossary_<N>.json 里的词条多半是上一轮（翻译或精修）自动产出的，
    # 而用户这次的要求很可能正是"这个词应该换个译法"；把它当既定译法注入
    # 会让模型不敢改。作品级 / 全局术语库照常注入（那是人工基准）。
    combined = store.merged_before_chapter(args.chapter)
    chapter_text = "\n".join(zh_paras)
    future_ctx = _future(work_dir, args.chapter, args.future)
    relevant = store.filter_for_content(combined, chapter_text, future_ctx)
    system_prompt = nkprompt.load_system_prompt(DEFAULT_PROMPT, work_dir)
    anchor = _anchor_text(work_dir, args.chapter, args.anchor)
    prev_ctx = _previous(work_dir, args.chapter, args.context)

    instruction = ""
    if args.instruction_file:
        if os.path.exists(args.instruction_file):
            instruction = nktext.read_text(args.instruction_file, "") or ""
            ui.info(f"已载入补充要求（{len(instruction.strip())} 字符）")
        else:
            ui.warn(f"补充要求文件不存在，已忽略: {args.instruction_file}")
    else:
        instruction = args.instruction or ""

    # 客户端提前创建：RAG 阶段要问一次模型提名关键词；dry-run 不需要
    client = None
    if not args.dry_run:
        try:
            client = llm.create_client(api_key=args.api_key, base_url=args.base_url, timeout=args.timeout)
        except llm.DependencyError as exc:
            ui.error(str(exc))
            return EXIT_DEPENDENCY
        except Exception as exc:  # noqa: BLE001
            ui.error(str(exc))
            return EXIT_FATAL

    research = ""
    if args.rag:
        researcher = nkrag.TermResearcher(str(work_dir))
        extra_terms: List[str] = []
        if args.rag_ask and client is not None:
            try:
                extra_terms = researcher.propose_terms(
                    chapter_text, relevant,
                    complete=lambda s, u: llm.ask_terms(
                        client, model=args.model, system=s, user=u
                    ),
                )
                if extra_terms:
                    ui.info("RAG 模型提名关键词: " + "、".join(extra_terms))
            except Exception as exc:  # noqa: BLE001
                ui.warn(f"RAG 模型提名失败，改用启发式候选: {exc}")
        try:
            findings = researcher.research(
                chapter_text, relevant, current_chapter=args.chapter, extra_terms=extra_terms
            )
            research = researcher.render(findings)
            if findings:
                ui.info("RAG 检索到 " + "、".join(f"{f.term}({len(f.snippets)})" for f in findings))
        except Exception as exc:  # noqa: BLE001
            ui.warn(f"RAG 检索失败，已跳过: {exc}")

    if args.dry_run:
        system, user = nkprompt.build_refine_messages(
            system_prompt, plan, relevant, anchor=anchor, prev_context=prev_ctx,
            future_context=future_ctx, research=research, extra_instruction=instruction,
            ignore_draft=args.ignore_draft)
        ui.info(f"[dry-run] 待修订 {len(plan)} 段；system {len(system)} 字符 / user {len(user)} 字符")
        print(user[:2000])
        return EXIT_OK

    batches = [plan[i:i + MAX_SEGMENTS] for i in range(0, len(plan), MAX_SEGMENTS)]
    ui.info(f"共 {len(plan)} 段，分 {len(batches)} 批处理")

    all_revisions: Dict[int, Dict[str, str]] = {}
    term_payload: Dict[str, Any] = {}
    failures: List[str] = []

    for index, batch in enumerate(batches, start=1):
        system, user = nkprompt.build_refine_messages(
            system_prompt, batch, relevant, anchor=anchor, prev_context=prev_ctx,
            future_context=future_ctx, research=research, extra_instruction=instruction,
            ignore_draft=args.ignore_draft)
        params = {
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

        def attempt():
            response = llm.call_chat(client, retries=0, **params)
            data = llm.extract_json(response.choices[0].message.content or "")
            if not isinstance(data, dict):
                raise llm.ResponseParseError("返回不是 JSON 对象")
            revisions = data.get("revisions")
            if not isinstance(revisions, list) or not revisions:
                raise llm.ResponseParseError("返回结果缺少 revisions 数组")
            return data

        try:
            data = llm.run_with_retry(
                attempt, retries=args.retries, base_delay=args.retry_delay,
                on_retry=lambda n, exc, delay: ui.warn(
                    f"第 {n} 次重试（{delay:.1f}s 后）：{str(exc).splitlines()[0][:120]}"))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"第 {index} 批：{exc}")
            ui.error(f"第 {index} 批失败：{exc}")
            continue

        parsed = [r for r in (parse_revision(item) for item in data["revisions"]) if r]
        wanted_rows = {seg["row"] for seg in batch}
        matched = [r for r in parsed if r["row"] in wanted_rows and r["text"]]

        if not matched and parsed:
            # 模型常常自己重新编号（返回 1,2,3 而不是真实行号）。
            # 数量与顺序都对得上时按顺序对号入座，避免整批白跑。
            usable = [r for r in parsed if r["text"]]
            if len(usable) == len(batch):
                ui.warn(f"第 {index} 批模型未按行号返回（返回 {[r['row'] for r in parsed]}），"
                        f"已按顺序对应到 {sorted(wanted_rows)}")
                matched = [{**r, "row": seg["row"]} for r, seg in zip(usable, batch)]

        if not matched:
            ui.error(f"第 {index} 批没有可用修订：请求行号 {sorted(wanted_rows)}，"
                     f"模型返回行号 {[r['row'] for r in parsed] or '（无）'}")
            if not parsed:
                ui.error(f"  模型返回片段：{str(data)[:400]}")
            failures.append(f"第 {index} 批：行号不匹配")
            continue

        for r in matched:
            all_revisions[r["row"]] = {"text": r["text"], "reason": r["reason"]}

        for key in ("new_fixed_terms", "new_contextual_terms",
                    "new_aesthetic_sentences", "new_cultural_nuances"):
            value = data.get(key)
            if isinstance(value, dict) and value:
                bucket = term_payload.setdefault(key, {})
                for term, item in value.items():
                    bucket.setdefault(term, item)

    if not all_revisions:
        ui.error("模型没有返回任何可用修订")
        for line in failures:
            ui.error(f"  {line}")
        return EXIT_PARTIAL

    new_en, notes, changed = apply_revisions(en_paras, rows, plan, all_revisions)
    nktext.write_text_atomic(str(en_path), "\n\n".join(new_en) + "\n")
    ui.ok(f"已写回译文：{len(notes)} 段有批注，其中 {changed} 段实际改动")

    # 批注落盘（按行合并，保留历史批注）
    notes_file = notes_path(work_dir, args.chapter)
    merged = merge_notes(load_notes(work_dir, args.chapter), notes)
    merged["chapter"] = args.chapter
    nktext.write_json_atomic(str(notes_file), merged)
    ui.info(f"批注已写入 {notes_file.name}（共 {len(merged['notes'])} 条）")

    # 术语入库：与整章翻译同一套规则
    proposed = nkglossary.build_chapter_glossary(term_payload)
    if any(proposed.values()):
        clean, conflicts = store.sanitize_chapter(
            args.chapter, proposed, allow_changes=args.allow_term_changes)
        if any(clean.values()):
            store.save_chapter(args.chapter, clean)
        if (work_dir / "global_glossary_tracker.json").exists() or any(clean.values()):
            store.write_tracker()
        store.record_conflicts(conflicts)
        blocked = [c for c in conflicts if c["action"] == "ignored"]
        if blocked:
            ui.warn(f"拦截 {len(blocked)} 条重复术语: "
                    + "、".join(f"{c['term']}→{c['old']}" for c in blocked[:4]))

    if failures:
        for line in failures:
            ui.error(f"  {line}")
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
