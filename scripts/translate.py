#!/usr/bin/env python3
"""网文翻译引擎（优化版）。

用法示例（与旧版完全兼容）：
    python main.py --dir two --model deepseek-v3.2 --anchor 2 --context -3 --future -1 \
        --thinking --temp 0.4 --top_p 0.7 --note --debug --chapter 2-3

相对旧版的主要改进
------------------
稳定性
  * API 调用带指数退避重试；"模型没返回合法 JSON"也会重试而不是终止整批任务。
  * 单章失败不再 `break` 掉整轮任务，默认继续翻译其余章节并汇总失败清单。
  * 请求参数按模型能力自动降级（response_format / enable_thinking / presence_penalty）。
  * 译文与术语库全部原子写入，进程被杀不会留下半截文件被误判为"已完成"。
  * 空译文/字段缺失会被判为失败并重试，绝不再写出空章节。
  * 显式设置请求超时，避免后台 nohup 任务永久挂起。

性能
  * 术语库文件读取由 O(n²) 降为 O(n)（带 mtime 失效的章节缓存）。
  * 全局 tracker 改为内存重建后落盘，不再每章重读全部 glossary 文件。
  * 日志句柄复用，不再每写一行开关一次文件。

质量
  * 术语过滤对英文键改用整词匹配，避免 "Li" 命中 "Liar" 之类的污染。
  * `--zh` 报告中文出现的行号，并可用 `--zh-retry` 自动重译修正。
  * 章节号解析同时支持空格与逗号（旧版传 "10,15,17" 会直接崩溃）。

可运维
  * `--dry-run` 离线演练，只组装提示词、不调用 API、不写文件。
  * `--reindex` 仅重建全局术语 tracker。
  * 结束时输出成功/失败/耗时/token 汇总，退出码可用于脚本判断。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelkit import config, llm  # noqa: E402
from novelkit import glossary as nkglossary  # noqa: E402
from novelkit import prompt as nkprompt  # noqa: E402
from novelkit import rag as nkrag  # noqa: E402
from novelkit import text as nktext  # noqa: E402
from novelkit import ui  # noqa: E402
from novelkit.ui import Color, c  # noqa: E402

GLOBAL_GLOSSARY = config.GLOBAL_GLOSSARY_PATH
DEFAULT_PROMPT = config.DEFAULT_PROMPT_PATH

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
EXIT_DEPENDENCY = 3


@dataclass
class ChapterOutcome:
    """单章翻译结果，用于结尾汇总。"""

    number: int
    status: str  # ok / failed / dry-run
    elapsed: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    salvaged: bool = False
    cjk_count: int = 0
    blocked_terms: int = 0
    rag_terms: int = 0
    message: str = ""


@dataclass
class RunStats:
    outcomes: List[ChapterOutcome] = field(default_factory=list)

    @property
    def ok(self) -> List[ChapterOutcome]:
        return [o for o in self.outcomes if o.status == "ok"]

    @property
    def failed(self) -> List[ChapterOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def total_prompt_tokens(self) -> int:
        return sum(o.prompt_tokens for o in self.outcomes)

    @property
    def total_completion_tokens(self) -> int:
        return sum(o.completion_tokens for o in self.outcomes)

    @property
    def elapsed(self) -> float:
        return sum(o.elapsed for o in self.outcomes)


class Translator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.work_dir = config.resolve_work_dir(args.dir)

        if not self.work_dir.is_dir():
            raise SystemExit(f"工作目录不存在: {self.work_dir}")

        # 日志
        log_file = ui.build_log_path(args.log_dir) if args.debug == "log" else None
        self.debug = ui.DebugLog(args.debug, log_file)
        self.log_file = log_file

        # 提示词
        self.system_prompt = self._load_system_prompt()

        # 使用者临时补充要求（<user_supplement>），来自 --instruction / --instruction-file
        self.instruction_text = _load_instruction(args)

        # 术语库：全局 config/glossary.json 作为最高权威一并交给 store 管理
        self.store = nkglossary.GlossaryStore(
            str(self.work_dir), str(GLOBAL_GLOSSARY)
        )

        # RAG 检索器（延迟到第一次真正用到时再加载全书语料）
        self._researcher: Optional[nkrag.TermResearcher] = None

        # 客户端延迟创建：--help / --dry-run 都不需要 API Key，也不需要 openai
        self.client = None
        self._variant_index: Optional[int] = None
        self._variant_labels: List[str] = []
        self.stats = RunStats()
        # 只有真正写入过章节术语库时才需要重写 tracker；
        # 否则一次"全部失败/空跑"也会凭空造出一个空的 tracker 文件。
        self._glossary_written = False

    def researcher(self) -> nkrag.TermResearcher:
        if self._researcher is None:
            args = self.args
            self._researcher = nkrag.TermResearcher(
                str(self.work_dir),
                scope=args.rag_scope,
                window=args.rag_window,
                snippets_per_term=args.rag_snippets,
                max_terms=args.rag_terms,
                budget=args.rag_budget,
            )
        return self._researcher

    # ------------------------------------------------------------------ 提示词

    def _load_system_prompt(self) -> str:
        try:
            return nkprompt.load_system_prompt(DEFAULT_PROMPT, self.work_dir)
        except FileNotFoundError as exc:
            raise SystemExit(str(exc))

    # ------------------------------------------------------------------ 上下文

    def anchor_text(self, target_num: int) -> str:
        parts: List[str] = []
        if self.args.anchor >= 1:
            intro = nktext.read_text(str(self.work_dir / "0_translated.txt"), "")
            if intro and intro.strip():
                parts.append(f"<work_introduction>\n{intro.strip()}\n</work_introduction>")
        if self.args.anchor == 2 and target_num > 1:
            first = nktext.read_text(str(self.work_dir / "1_translated.txt"), "")
            if first and first.strip():
                words = first.strip().split()
                parts.append(f"<first_chapter_sample>\n{' '.join(words[:500])}\n</first_chapter_sample>")
        return "\n".join(parts)

    def _unit(self, default: str) -> str:
        return default if self.args.unit == "auto" else self.args.unit

    def previous_context(self, target_num: int) -> str:
        param = self.args.context
        if param == 0 or target_num <= 1:
            return ""

        if param > 0:
            unit = self._unit("words")
            remaining = param
            chunks: List[str] = []
            for index in range(target_num - 1, 0, -1):
                text = nktext.read_text(str(self.work_dir / f"{index}_translated.txt"), "")
                if not text or not text.strip():
                    continue
                size = nktext.count_units(text, unit)
                if size <= remaining:
                    chunks.insert(0, text.strip())
                    remaining -= size
                else:
                    chunks.insert(0, nktext.take_tail(text, remaining, unit))
                    remaining = 0
                if remaining <= 0:
                    break
            return "\n\n".join(chunks)

        needed = abs(param)
        chunks = []
        for index in range(target_num - 1, 0, -1):
            text = nktext.read_text(str(self.work_dir / f"{index}_translated.txt"), "")
            if text and text.strip():
                chunks.insert(0, text.strip())
                needed -= 1
            if needed <= 0:
                break
        return "\n\n".join(chunks)

    def future_context(self, target_num: int) -> str:
        param = self.args.future
        if param == 0:
            return ""

        chunks: List[str] = []
        index = target_num + 1

        if param > 0:
            unit = self._unit("chars")
            remaining = param
            while remaining > 0:
                raw = nktext.read_text(str(self.work_dir / f"{index}_origin.txt"))
                if raw is None:
                    break
                text = raw.strip()
                if not text:
                    index += 1
                    continue
                size = nktext.count_units(text, unit)
                if size <= remaining:
                    chunks.append(text)
                    remaining -= size
                else:
                    chunks.append(nktext.take_head(text, remaining, unit))
                    remaining = 0
                index += 1
            return "\n\n".join(chunks)

        needed = abs(param)
        while needed > 0:
            raw = nktext.read_text(str(self.work_dir / f"{index}_origin.txt"))
            if raw is None:
                break
            if raw.strip():
                chunks.append(raw.strip())
                needed -= 1
            index += 1
        return "\n\n".join(chunks)

    # -------------------------------------------------------------- 提示词组装

    # RAG 使用说明与消息组装统一放在 novelkit.prompt，
    # 保证整章翻译与局部重译（retranslate.py）注入的内容完全一致。

    def build_messages(
        self,
        content: str,
        relevant_glossary: Dict[str, Any],
        anchor: str,
        prev_context: str,
        future_context: str,
        draft: Optional[str] = None,
        feedback: Optional[str] = None,
        research: str = "",
    ) -> Tuple[str, str]:
        return nkprompt.build_messages(
            self.system_prompt, content, relevant_glossary,
            anchor=anchor, prev_context=prev_context, future_context=future_context,
            draft=draft, feedback=feedback, research=research,
            extra_instruction=self.instruction_text,
        )

    # ------------------------------------------------------------ 请求参数降级

    def _param_variants(self, model: str, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """构造从"功能最全"到"最保守"的若干组请求参数。

        日志显示同一套脚本跑过 qwen3-max / deepseek-r1 / deepseek-v3.2，
        它们对 response_format、enable_thinking、presence_penalty 的支持并不一致。
        逐个降级比直接报 400 更省事，而且成功的那一组会被记住复用。
        """
        extra_body = {
            "repetition_penalty": self.args.repetition_penalty,
            "enable_thinking": bool(self.args.thinking),
            "thinking_budget": self.args.thinking_budget,
        }

        def make(*, presence: bool, json_mode: bool, extra: bool) -> Dict[str, Any]:
            params: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": self.args.temp,
                "top_p": self.args.top_p,
            }
            if presence:
                params["presence_penalty"] = self.args.presence_penalty
            if json_mode and self.args.json_mode:
                params["response_format"] = {"type": "json_object"}
            if extra:
                params["extra_body"] = extra_body
            return params

        plan = [
            ("完整参数", make(presence=True, json_mode=True, extra=True)),
            ("去掉 extra_body", make(presence=True, json_mode=True, extra=False)),
            ("去掉 response_format", make(presence=True, json_mode=False, extra=True)),
            ("仅基础参数", make(presence=True, json_mode=False, extra=False)),
            ("去掉 presence_penalty", make(presence=False, json_mode=True, extra=False)),
            ("最保守参数", make(presence=False, json_mode=False, extra=False)),
        ]

        unique: List[Dict[str, Any]] = []
        self._variant_labels = []
        for label, params in plan:
            if params not in unique:
                unique.append(params)
                self._variant_labels.append(label)
        return unique

    def _create(self, model: str, messages: List[Dict[str, str]]):
        """发起一次请求；遇到"参数不被支持"时自动切换到更保守的参数组。"""
        variants = self._param_variants(model, messages)
        order = list(range(len(variants)))
        if self._variant_index is not None and self._variant_index < len(variants):
            order.remove(self._variant_index)
            order.insert(0, self._variant_index)

        last_error: Optional[BaseException] = None
        for position, index in enumerate(order):
            try:
                response = self.client.chat.completions.create(**variants[index])
                if self._variant_index != index:
                    self.debug.write(f"[参数] 采用参数组: {self._variant_labels[index]}")
                    self._variant_index = index
                return response
            except BaseException as exc:  # noqa: BLE001
                last_error = exc
                if position >= len(order) - 1 or not llm.is_param_error(exc):
                    raise

                # 根据报错内容直接挑掉"不被支持"的参数，避免逐个盲试
                offenders = llm.param_error_hint(exc)
                remaining = order[position + 1:]
                if offenders:
                    remaining.sort(key=lambda i: len(offenders & set(variants[i])))
                self.debug.write(
                    f"[参数] 参数组「{self._variant_labels[index]}」被拒绝({exc})，"
                    f"降级重试（问题参数: {sorted(offenders) or '未知'}）"
                )
                for offset, next_index in enumerate(remaining):
                    order[position + 1 + offset] = next_index
        raise last_error  # pragma: no cover

    def _ask_model_for_terms(self, content: str, relevant: Dict[str, Any]) -> List[str]:
        """额外调一次模型，让它自己提名本章值得检索语境的词。

        只负责发请求；提示词与"必须出现在正文里"的校验都在 novelkit.rag 里，
        保证命令行、面板与各脚本行为一致。失败由调用方捕获，不影响翻译。
        """
        if self.client is None:
            return []

        def complete(system: str, user: str) -> str:
            response = self._create(
                self.args.model,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            message = response.choices[0].message
            return getattr(message, "content", "") or ""

        return self.researcher().propose_terms(content, relevant, complete=complete)

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _parse_payload(text: str, reasoning: str) -> Tuple[Dict[str, Any], bool]:
        """把模型输出解析为规范 payload。返回 (payload, 是否走了纯文本兜底)。"""
        data = None
        if text and text.strip():
            try:
                data = llm.extract_json(text)
            except llm.ResponseParseError:
                data = None

        if data is None:
            salvaged = llm.salvage_plain_translation(text) if text else None
            if salvaged is not None:
                return salvaged, True
            if reasoning and reasoning.strip():
                try:
                    data = llm.extract_json(reasoning)
                except llm.ResponseParseError:
                    data = None

        if data is None:
            raise llm.ResponseParseError(
                "模型返回内容无法解析为 JSON（已尝试围栏剥离/平衡括号扫描/思考内容兜底）",
                raw=text or "",
                reasoning=reasoning or "",
            )

        payload = llm.coerce_translation_payload(data)
        if not payload.get("translated_content", "").strip():
            # 关键保护：旧版会把空字符串当成译文写出一个空章节文件
            raise llm.ResponseParseError("模型返回的 translated_content 为空", raw=text or "")
        return payload, False

    def _invoke(
        self,
        model: str,
        system: str,
        user: str,
        *,
        want_json: bool,
    ) -> Tuple[Any, str, str, Dict[str, int], bool]:
        """带重试的一次 LLM 调用，返回 (结果, 原文, 思考, usage, 是否兜底)。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        def attempt():
            response = self._create(model, messages)
            message = response.choices[0].message
            text = message.content or ""
            reasoning = getattr(message, "reasoning_content", None) or ""
            usage = llm.extract_usage(response)

            if want_json:
                payload, salvaged = self._parse_payload(text, reasoning)
                return payload, text, reasoning, usage, salvaged

            if not text.strip():
                raise llm.ResponseParseError("模型返回内容为空", reasoning=reasoning)
            return text, text, reasoning, usage, False

        def on_retry(attempt_no: int, exc: BaseException, delay: float) -> None:
            raw = str(exc).splitlines()
            short = raw[0][:120] if raw and raw[0] else type(exc).__name__
            ui.warn(f"第 {attempt_no} 次重试（{delay:.1f}s 后）：{short}")
            self.debug.write(f"[重试] attempt={attempt_no} delay={delay:.2f}s error={short}")

        payload, text, reasoning, usage, salvaged = llm.run_with_retry(
            attempt,
            retries=self.args.retries,
            base_delay=self.args.retry_delay,
            on_retry=on_retry,
        )
        if salvaged:
            ui.warn("模型未返回 JSON，已将正文按纯文本兜底采纳（建议检查该章）")
        return payload, text, reasoning, usage, salvaged

    # ------------------------------------------------------------------ 单章

    def translate_chapter(self, target_num: int) -> ChapterOutcome:
        args = self.args
        started = time.monotonic()
        target_path = self.work_dir / f"{target_num}_origin.txt"

        if not target_path.exists():
            return ChapterOutcome(target_num, "failed", message=f"找不到源文件 {target_path}")

        original_content = target_path.read_text(encoding="utf-8")
        if not original_content.strip():
            return ChapterOutcome(target_num, "failed", message="源文件为空")

        # merged_up_to 已经内含全局 glossary.json（全局优先）
        combined_glossary = self.store.merged_up_to(target_num)

        anchor = self.anchor_text(target_num)
        prev_context = self.previous_context(target_num)
        future_context = self.future_context(target_num)
        relevant = self.store.filter_for_content(combined_glossary, original_content, future_context)

        # ---- RAG：为"可能有特定内涵"的候选词检索全书（默认后文）语境
        research_block = ""
        if args.rag:
            extra_terms: List[str] = []
            # 先让模型自己提名关键词；这一步失败不影响启发式检索
            if args.rag_ask and self.client is not None and not args.dry_run:
                try:
                    extra_terms = self._ask_model_for_terms(original_content, relevant)
                    if extra_terms:
                        ui.info("RAG 模型提名关键词: " + "、".join(extra_terms))
                except Exception as exc:  # noqa: BLE001
                    ui.warn(f"RAG 模型提名失败，改用启发式候选: {_short_error(exc)}")
            try:
                findings = self.researcher().research(
                    original_content, relevant, current_chapter=target_num,
                    extra_terms=extra_terms,
                )
                research_block = self.researcher().render(findings)
                if findings:
                    ui.info(
                        "RAG 检索到 "
                        + "、".join(f"{f.term}({len(f.snippets)})" for f in findings)
                    )
            except Exception as exc:  # noqa: BLE001 - 检索失败不应中断翻译
                ui.warn(f"RAG 检索失败，已跳过: {_short_error(exc)}")

        if args.note and args.note >= 2:
            self._print_glossary(relevant)

        system, user = self.build_messages(
            original_content, relevant, anchor, prev_context, future_context,
            research=research_block,
        )

        if args.dry_run:
            self._print_dry_run(
                target_num, system, user, relevant, original_content, research_block
            )
            return ChapterOutcome(target_num, "dry-run", elapsed=time.monotonic() - started)

        self.debug.banner(f"[DEBUG] 第 {target_num} 章请求")
        self.debug.write(f"模型: {args.model}")
        self.debug.write(
            f"参数: temp={args.temp} top_p={args.top_p} presence={args.presence_penalty} "
            f"repetition={args.repetition_penalty} thinking={args.thinking}"
        )
        self.debug.write(f"【System Prompt】\n{system}\n")
        self.debug.write(f"【User Prompt】\n{user}")

        usage_total: Dict[str, int] = {}
        payload, text, reasoning, usage, salvaged = self._invoke(
            args.model, system, user, want_json=True
        )
        _accumulate(usage_total, usage)
        translated = payload.get("translated_content", "")
        self._log_response(target_num, text, reasoning)

        # ---- 自动评审 + 重译
        # 评审/重译属于"锦上添花"，失败时绝不能丢掉已经拿到手的初译。
        if args.refine:
            refined = self._try_refine(
                target_num, original_content, translated, payload, relevant,
                anchor, prev_context, future_context, usage_total,
                research=research_block,
            )
            if refined is not None:
                payload, translated = refined

        # ---- 中文残留检测与自动修正（仅在显式要求重译时进入）
        if args.zh_retry > 0:
            translated, payload, fix_usage = self._enforce_english(
                target_num, translated, payload, relevant, anchor,
                prev_context, future_context,
                content=original_content, retries=args.zh_retry,
                research=research_block,
            )
            _accumulate(usage_total, fix_usage)

        # ---- 规范化标点后原子写入
        final_text = nktext.normalize_punctuation(
            translated, strip_asterisk=not args.keep_asterisk
        )
        # 首行章节标题格式必须全书统一（`Chapter NN: 标题`）。模型偶尔漏掉冒号，
        # 甚至整行吞掉标题；这里显式告警，避免问题静默写进文件。
        heading_problem = nktext.check_chapter_heading(original_content, final_text)
        if heading_problem:
            ui.warn(f"第 {target_num} 章 {heading_problem}")
        output_path = self.work_dir / f"{target_num}_translated.txt"
        nktext.write_text_atomic(str(output_path), final_text)

        # ---- 术语入库（落盘前拦截"重复输出已有术语"造成的冲突）
        proposed_glos = nkglossary.build_chapter_glossary(payload)
        chapter_glos, conflicts = self.store.sanitize_chapter(
            target_num, proposed_glos, allow_changes=args.allow_term_changes
        )

        if args.note and args.note >= 1:
            self._print_extracted(payload, combined_glossary, target_num, conflicts)

        # 术语库属于派生数据：写失败要吵闹地告警，但不能因此否定已完成的译文
        glossary_warning = ""
        try:
            self.store.save_chapter(target_num, chapter_glos,
                                    merge=not args.rebuild_glossary)
            tracker_path = str(self.work_dir / "global_glossary_tracker.json")
            self.store.write_tracker()
            self.store.record_conflicts(conflicts)
            self._glossary_written = True
        except Exception as exc:  # noqa: BLE001
            glossary_warning = _short_error(exc)
            tracker_path = "未写入"
            ui.error(f"第 {target_num} 章术语库写入失败（译文已保存）: {glossary_warning}")

        blocked = [x for x in conflicts if x["action"] == "ignored"]
        if blocked:
            ui.warn(
                f"第 {target_num} 章有 {len(blocked)} 条重复术语被拦截并沿用既定译法"
                f"（详见 {nkglossary.CONFLICTS_FILENAME}）: "
                + "、".join(f"{x['term']}→{x['old']}" for x in blocked[:4])
                + ("…" if len(blocked) > 4 else "")
            )
        kept = [x for x in conflicts if x["action"] == "overridden"]
        if kept:
            ui.warn(
                f"第 {target_num} 章按 --allow-term-changes 覆盖了 {len(kept)} 条既定译法；"
                "第 " + str(target_num) + " 章之后的正文可能仍在使用旧译法，请人工核对。"
            )

        issues = nktext.detect_issues(final_text)
        if args.zh:
            if issues["has_cjk"]:
                ui.warn(
                    f"第 {target_num} 章译文仍残留中文 {issues['cjk_count']} 处，"
                    f"行号: {_preview_lines(issues['cjk_lines'])}"
                )
            else:
                ui.info(f"第 {target_num} 章中文检测: 无中文")

        term_count = sum(len(v) for v in chapter_glos.values())
        ui.ok(
            f"第 {target_num} 章翻译完成 -> {output_path.name} "
            f"({len(final_text)} 字符, 本章新术语 {term_count} 条)"
        )

        # ---- 回译 / 评审
        if args.back or args.review:
            self._run_checks(target_num, original_content, final_text, chapter_glos)

        return ChapterOutcome(
            target_num,
            "ok",
            elapsed=time.monotonic() - started,
            prompt_tokens=usage_total.get("prompt_tokens", 0),
            completion_tokens=usage_total.get("completion_tokens", 0),
            salvaged=salvaged,
            cjk_count=issues["cjk_count"],
            blocked_terms=len(blocked),
            rag_terms=len(findings) if args.rag and research_block else 0,
            message=(
                f"术语库写入失败: {glossary_warning}" if glossary_warning
                else f"tracker={Path(tracker_path).name}"
            ),
        )

    # ------------------------------------------------------------ 子步骤

    def _try_refine(
        self,
        target_num: int,
        original_content: str,
        translated: str,
        payload: Dict[str, Any],
        relevant: Dict[str, Any],
        anchor: str,
        prev_context: str,
        future_context: str,
        usage_total: Dict[str, int],
        research: str = "",
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """评审 + 重译。任何一步失败都保留初译，返回 None。"""
        args = self.args
        try:
            ui.info("正在进行自动评审...")
            review_model = args.model_review or args.model
            tmp_glos = {
                "fixed_terms": payload.get("new_fixed_terms", {}),
                "contextual_terms": payload.get("new_contextual_terms", {}),
            }
            feedback = self._review(
                review_model, original_content, translated,
                json.dumps(tmp_glos, ensure_ascii=False),
            )
            if not feedback or not feedback.strip():
                ui.warn("评审返回为空，保留初译。")
                return None

            ui.info("评审完成，正在基于评审结果重新翻译...")
            system2, user2 = self.build_messages(
                original_content, relevant, anchor, prev_context, future_context,
                draft=translated, feedback=feedback, research=research,
            )
            payload2, text2, reasoning2, usage2, _ = self._invoke(
                args.model, system2, user2, want_json=True
            )
            _accumulate(usage_total, usage2)
            self._log_response(target_num, text2, reasoning2)

            candidate = payload2.get("translated_content", "")
            if not candidate.strip():
                ui.warn("重译结果为空，保留初译。")
                return None
            return payload2, candidate
        except Exception as exc:  # noqa: BLE001
            ui.warn(f"评审/重译失败，保留初译: {_short_error(exc)}")
            return None

    def _enforce_english(
        self,
        target_num: int,
        translated: str,
        payload: Dict[str, Any],
        relevant: Dict[str, Any],
        anchor: str,
        prev_context: str,
        future_context: str,
        *,
        content: str,
        retries: int,
        research: str = "",
    ) -> Tuple[str, Dict[str, Any], Dict[str, int]]:
        """若译文残留中文则按 --zh-retry 指定的次数重译。"""
        usage_total: Dict[str, int] = {}
        attempt = 0
        while True:
            issues = nktext.detect_issues(translated)
            if not issues["has_cjk"]:
                break
            if attempt >= retries:
                ui.warn(
                    f"第 {target_num} 章仍检测到中文 {issues['cjk_count']} 处"
                    f"（行号 {_preview_lines(issues['cjk_lines'])}），已放弃自动修正"
                )
                break
            attempt += 1
            ui.warn(
                f"第 {target_num} 章检测到中文 {issues['cjk_count']} 处，"
                f"正在进行第 {attempt} 次修正重译..."
            )
            system2, user2 = self.build_messages(
                content, relevant, anchor, prev_context, future_context, research=research
            )
            user2 += (
                f"\n\n<draft_translation>\n{translated}\n</draft_translation>\n\n"
                f"<review_feedback>\n上一版译文中仍残留 {issues['cjk_count']} 处中文"
                f"（行号 {_preview_lines(issues['cjk_lines'])}）。"
                f"请重新翻译，确保输出为纯英文，不得出现任何中文字符。\n</review_feedback>\n"
            )
            new_payload, text2, reasoning2, usage2, _ = None, "", "", {}, False
            try:
                new_payload, text2, reasoning2, usage2, _ = self._invoke(
                    self.args.model, system2, user2, want_json=True
                )
            except Exception as exc:  # noqa: BLE001 - 修正失败不应丢掉现有译文
                ui.warn(f"第 {target_num} 章修正重译失败，保留当前译文: {_short_error(exc)}")
                break
            _accumulate(usage_total, usage2)
            self._log_response(target_num, text2, reasoning2)

            candidate = new_payload.get("translated_content", "")
            if not candidate.strip():
                break
            translated = candidate
            payload = new_payload
        return translated, payload, usage_total

    def _review(self, model: str, origin: str, translated: str, glossary_json: str) -> str:
        system = "你是一个专业的翻译评审专家，尤其需要找出不足以帮助改进。"
        user = f"请评价以下翻译的质量。\n原文:\n{origin}\n\n译文:\n{translated}\n\n术语表:\n{glossary_json}"
        text, _raw, _reasoning, _usage, _salvaged = self._invoke(model, system, user, want_json=False)
        return text

    def _run_checks(self, target_num: int, origin: str, translated: str, chapter_glos: Dict) -> None:
        try:
            from checker import get_client as get_async_client
            from checker import run_back, run_review
        except Exception as exc:  # noqa: BLE001
            ui.warn(f"无法加载 checker 模块，跳过回译/评审: {exc}")
            return

        args = self.args
        back_path = str(self.work_dir / f"{target_num}_back.txt")
        review_path = str(self.work_dir / f"{target_num}_review.txt")
        glossary_content = json.dumps(chapter_glos, ensure_ascii=False, indent=2)

        async def runner():
            client = get_async_client(
                api_key=args.api_key, base_url=args.base_url, timeout=args.timeout
            )
            tasks = []
            if args.back:
                tasks.append(
                    run_back(client, translated, args.model_back or args.model, back_path,
                             retries=args.retries)
                )
            if args.review:
                tasks.append(
                    run_review(client, origin, translated, glossary_content,
                               args.model_review or args.model, review_path, retries=args.retries)
                )
            if tasks:
                await asyncio.gather(*tasks)

        try:
            asyncio.run(runner())
        except Exception as exc:  # noqa: BLE001 - 回译/评审失败不影响正文
            ui.warn(f"回译/评审失败（正文已保存）: {exc}")

    # ------------------------------------------------------------ 输出辅助

    def _print_glossary(self, relevant: Dict[str, Any]) -> None:
        print(f"\n{c('+--- [ GLOSSARY ] ---+', Color.BOLD + Color.CYAN)}")
        simple = {
            "fixed_terms": relevant.get("fixed_terms", {}),
            "contextual_terms": relevant.get("contextual_terms", {}),
        }
        print(c(json.dumps(simple, ensure_ascii=False, indent=2), Color.CYAN))
        print(c("+--------------------+", Color.CYAN))

    def _print_dry_run(self, target_num, system, user, relevant, content, research="") -> None:
        ui.info(f"[dry-run] 第 {target_num} 章")
        print(f"  原文长度      : {len(content)} 字符")
        print(f"  System Prompt : {len(system)} 字符")
        print(f"  User Prompt   : {len(user)} 字符")
        print(f"  命中术语      : "
              f"fixed={len(relevant.get('fixed_terms', {}))} "
              f"contextual={len(relevant.get('contextual_terms', {}))} "
              f"aesthetic={len(relevant.get('aesthetic_sentences', {}))} "
              f"culture={len(relevant.get('cultural_nuances', {}))}")
        if research:
            print(f"  RAG 检索块    : {len(research)} 字符, "
                  f"{research.count('<term ')} 个候选词")
            print(c(research[:1500] + ("\n...[截断]" if len(research) > 1500 else ""), Color.BLUE))
        print(f"  {'-' * 46}")
        preview = user if len(user) <= 1200 else user[:1200] + "\n...[截断]"
        print(c(preview, Color.GREY))
        print(f"  {'-' * 46}")
        self.debug.write(f"[dry-run] 第 {target_num} 章 System:\n{system}\n\nUser:\n{user}")

    def _log_response(self, target_num, text, reasoning) -> None:
        if self.args.output and reasoning and reasoning.strip():
            print(f"\n{c('+--- [ AI THINKING ] ---+', Color.BOLD + Color.BLUE)}")
            print(c(reasoning.strip(), Color.BLUE))
            print(c("+-----------------------+", Color.BLUE) + "\n")
        self.debug.write(f"\n===== [DEBUG] 第 {target_num} 章返回 =====")
        if reasoning and reasoning.strip():
            self.debug.write(f"【思考内容】\n{reasoning}\n")
        self.debug.write(f"【正文内容】\n{text}")
        self.debug.write("=" * 40)

    def _print_extracted(self, payload, combined_glossary, target_num, conflicts) -> None:
        new_fixed = payload.get("new_fixed_terms") or {}
        new_fixed = {k: v for k, v in new_fixed.items() if k != v and not nktext.has_cjk(v)}
        new_ctx = payload.get("new_contextual_terms") or {}

        print(f"\n{c('+--- [ EXTRACT ] ---+', Color.BOLD + Color.YELLOW)}")
        if new_fixed:
            print(c("[ Fixed Terms ]", Color.GREEN))
            for term, trans in new_fixed.items():
                print(f" > {c(term, Color.BOLD + Color.CYAN)} -> {c(trans, Color.YELLOW)}")
                for key, value in combined_glossary.get("fixed_terms", {}).items():
                    if term in key and key != term:
                        print(f"   |- Fixed: \n   |-- {c(key, Color.CYAN)} -> {c(value, Color.YELLOW)}")
                for key, value in combined_glossary.get("contextual_terms", {}).items():
                    if term in key:
                        print(f"   |-- Ctx: \n   |-- {c(key, Color.CYAN)} -> {c(value, Color.YELLOW)}")

        if new_ctx:
            print(f"\n{c('[ Contextual Terms ]', Color.BLUE)}")
            for term, contexts in new_ctx.items():
                print(f" > {c(term, Color.BOLD + Color.CYAN)}")
                for ctx_key, ctx_val in (contexts or {}).items():
                    print(f"   |-- {c(ctx_key, Color.CYAN)} -> {c(ctx_val, Color.YELLOW)}")
                for key, value in combined_glossary.get("fixed_terms", {}).items():
                    if term in key:
                        print(f"   |-- {c('[!] Fixed exist:', Color.RED)} \n"
                              f"   |-- {c(key, Color.CYAN)} -> {c(value, Color.YELLOW)}")
                for key, value in combined_glossary.get("contextual_terms", {}).items():
                    if term in key:
                        print(f"   |-- Ctx exist: \n   |-- {c(key, Color.CYAN)} -> {c(value, Color.YELLOW)}")

        if conflicts:
            print(f"\n{c('[ 冲突拦截 ]', Color.RED + Color.BOLD)}")
            for item in conflicts:
                origin = "全局术语库" if item["origin"] == "global" else f"第 {item['origin_chapter']} 章"
                tag = c("已覆盖", Color.YELLOW) if item["action"] == "overridden" else c("已拦截", Color.RED)
                where = f"/{item['context']}" if item.get("context") else ""
                note = "（仅大小写差异）" if item.get("severity") == "minor" else ""
                print(
                    f" {c('[!]', Color.RED)} [{tag}] {item['category']} {item['term']}{where}{note}\n"
                    f"     既定译法: {c(item['old'], Color.GREEN)}  （{origin}）\n"
                    f"     模型新给: {c(item['new'], Color.RED)}"
                )
        print(c("+-------------------+", Color.YELLOW) + "\n")

    # ------------------------------------------------------------------ 主流程

    def select_targets(self) -> List[int]:
        args = self.args

        if args.chapter:
            return nktext.parse_chapters(args.chapter) or []

        available = nktext.chapter_files(str(self.work_dir), "_origin.txt")
        if args.force:
            pending = available
        else:
            pending = [
                num for num in available
                if not (self.work_dir / f"{num}_translated.txt").exists()
            ]
        return pending[: max(0, args.tasks)]

    def run(self) -> int:
        args = self.args
        targets = self.select_targets()

        if not targets:
            ui.info("没有需要翻译的章节。")
            return EXIT_OK

        ui.info(
            f"工作目录: {self.work_dir}\n"
            f"模型: {args.model} | 待翻译章节: {nktext.format_chapter_list(targets)}"
        )

        if self.debug.enabled:
            self.debug.banner("[DEBUG] 参数信息")
            for key, value in sorted(vars(args).items()):
                self.debug.write(f"{key}: {value}")
            self.debug.write(f"log_file: {self.log_file}")
            self.debug.write("=" * 40)

        interrupted = False
        try:
            for position, number in enumerate(targets, start=1):
                print(f"\n{c(f'== 第 {number} 章 [{position}/{len(targets)}] ==', Color.BOLD + Color.HEADER)}")
                try:
                    outcome = self.translate_chapter(number)
                except KeyboardInterrupt:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 单章失败不影响整批
                    detail_lines = str(exc).splitlines()
                    detail = detail_lines[0][:300] if detail_lines and detail_lines[0] else type(exc).__name__
                    outcome = ChapterOutcome(
                        number, "failed", message=f"{type(exc).__name__}: {detail}"
                    )
                    ui.error(f"第 {number} 章失败: {outcome.message}")

                self.stats.outcomes.append(outcome)

                if outcome.status == "failed" and args.fail_fast:
                    ui.error("已按 --fail-fast 中止后续任务。")
                    break
        except KeyboardInterrupt:
            interrupted = True
            ui.warn("收到中断信号，正在保存术语 tracker 后退出...")
        finally:
            # dry-run 的约定是"不写任何文件"，tracker 也不能例外；
            # 另外只有本轮真的写过术语库时才重写，避免空跑造出空 tracker。
            tracker_exists = (self.work_dir / "global_glossary_tracker.json").exists()
            if not args.dry_run and (self._glossary_written or tracker_exists):
                try:
                    self.store.write_tracker()
                except Exception as exc:  # noqa: BLE001
                    ui.warn(f"tracker 写入失败: {exc}")
            self.debug.close()

        self._print_summary()
        if interrupted:
            return EXIT_PARTIAL
        return EXIT_OK if not self.stats.failed else EXIT_PARTIAL

    def _print_summary(self) -> None:
        stats = self.stats
        dry = [o for o in stats.outcomes if o.status == "dry-run"]
        if dry and not stats.ok and not stats.failed:
            ui.info(
                f"[dry-run] 已演练 {len(dry)} 章："
                f"{nktext.format_chapter_list([o.number for o in dry])}"
            )
            return

        print(f"\n{c('=' * 18 + ' 本次任务汇总 ' + '=' * 18, Color.BOLD)}")
        print(f"  成功: {c(str(len(stats.ok)), Color.GREEN)} 章"
              f" | 失败: {c(str(len(stats.failed)), Color.RED)} 章"
              f" | 耗时: {stats.elapsed:.1f}s")
        if stats.total_prompt_tokens or stats.total_completion_tokens:
            print(f"  tokens: prompt={stats.total_prompt_tokens:,} "
                  f"completion={stats.total_completion_tokens:,} "
                  f"合计={stats.total_prompt_tokens + stats.total_completion_tokens:,}")
        salvaged = [o.number for o in stats.outcomes if o.salvaged]
        if salvaged:
            ui.warn("以下章节走了纯文本兜底（模型未返回 JSON），建议人工检查: "
                    f"{nktext.format_chapter_list(salvaged)}")
        cjk = [(o.number, o.cjk_count) for o in stats.ok if o.cjk_count]
        if cjk:
            ui.warn("以下章节仍含中文: " + ", ".join(f"{n}({k}处)" for n, k in cjk))
        for outcome in stats.failed:
            ui.error(f"  失败章节 {outcome.number}: {outcome.message}")
        print(c("=" * 52, Color.BOLD))


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _accumulate(total: Dict[str, int], extra: Dict[str, int]) -> None:
    for key, value in (extra or {}).items():
        total[key] = total.get(key, 0) + value


def _load_instruction(args: argparse.Namespace) -> str:
    """读取补充要求：--instruction-file 优先（面板用它传长文本）。"""
    path = getattr(args, "instruction_file", None)
    if path:
        if not os.path.exists(path):
            ui.warn(f"补充要求文件不存在，已忽略: {path}")
            return getattr(args, "instruction", "") or ""
        text = nktext.read_text(path, "") or ""
        ui.info(f"已载入补充要求（{len(text.strip())} 字符）")
        return text
    return getattr(args, "instruction", "") or ""


def _short_error(exc: BaseException) -> str:
    """把异常压成一行，便于放进日志与汇总。"""
    lines = str(exc).splitlines()
    return lines[0][:200] if lines and lines[0] else type(exc).__name__


def _preview_lines(lines: List[int], limit: int = 12) -> str:
    if not lines:
        return "无"
    if len(lines) <= limit:
        return ", ".join(str(x) for x in lines)
    return ", ".join(str(x) for x in lines[:limit]) + f" ... 共 {len(lines)} 行"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="网文翻译工具（优化版）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    base_group = parser.add_argument_group("核心配置 (Core)")
    base_group.add_argument("--dir", required=True, metavar="PATH",
                            help="作品目录（相对路径优先在 works/ 工作区里查找）")
    base_group.add_argument("--model", default=config.get_model(),
                            help=f"指定的模型 ID（默认取 .env 的 API_MODEL，否则 {config.DEFAULT_MODEL}）")
    base_group.add_argument("--model_back", type=str, default=None, help="指定回译使用的模型，不提供则默认使用翻译模型")
    base_group.add_argument("--model_review", type=str, default=None, help="指定评审使用的模型，不提供则默认使用翻译模型")
    base_group.add_argument("--base_url", default=config.get_base_url(),
                            help=f"API 地址（默认取 .env 的 API_BASE_URL，否则 {config.DEFAULT_BASE_URL}）")
    base_group.add_argument("--api_key", default=None, help="API Key（默认读取 .env 的 API_KEY）")

    task_logic_group = base_group.add_mutually_exclusive_group(required=False)
    task_logic_group.add_argument("--tasks", type=int, default=1, metavar="N", help="本次翻译的任务数量")
    task_logic_group.add_argument("--chapter", type=str, default=None, metavar="STR",
                                  help="指定翻译章节，支持区间与混合，如 '1-4 6 8-9' 或 '1,3,5-10'")

    strategy_group = parser.add_argument_group("翻译上下文与风格 (Strategy)")
    strategy_group.add_argument("--context", type=int, default=500, metavar="VAL",
                                help="参考前文内容：正数为长度，负数为章节数")
    strategy_group.add_argument("--future", type=int, default=0, metavar="VAL",
                                help="参考后文内容：建议值 200-500，用于解决性别或伏笔歧义")
    strategy_group.add_argument("--anchor", type=int, choices=[0, 1, 2], default=1,
                                help="风格基准强度：0-不参考, 1-仅简介, 2-简介+首章采样")
    strategy_group.add_argument("--unit", choices=["auto", "words", "chars"], default="auto",
                                help="context/future 正数长度的计量单位（auto 保持旧版行为：前文按词、后文按字符）")

    param_group = parser.add_argument_group("模型微调 (Hyperparameters)")
    param_group.add_argument("--temp", type=float, default=0.7, help="采样温度：越高越有文采，越低越严谨稳定")
    param_group.add_argument("--top_p", type=float, default=0.8, help="核采样阈值：越低越不会采用生僻词")
    param_group.add_argument("--presence_penalty", type=float, default=0.3, help="存在惩罚：防止 AI 陷入特定短语或句式的死循环")
    param_group.add_argument("--repetition_penalty", type=float, default=1.1, help="重复惩罚：针对非 OpenAI 系模型的重复控制")

    extra_group = parser.add_argument_group("高级功能 (Advanced)")
    extra_group.add_argument("--thinking", action="store_true", help="启用模型思考过程")
    extra_group.add_argument("--thinking_budget", type=int, default=8192, help="思考 Token 预算：仅在 --thinking 开启时有效")
    extra_group.add_argument("--zh", action="store_true", help="检查译文中是否包含中文（会报告行号）")
    extra_group.add_argument("--zh-retry", type=int, default=0, metavar="N",
                             help="检测到中文残留时自动重译的最大次数（0 表示只报告不重译）")
    extra_group.add_argument("--back", action="store_true", help="翻译后自动执行回译")
    extra_group.add_argument("--review", action="store_true", help="翻译后自动执行 AI 评审")
    extra_group.add_argument("--refine", action="store_true", help="完成一次翻译后进行评审，并基于评审结果重新翻译作为最终结果")
    extra_group.add_argument("--debug", nargs="?", const="log", choices=["console", "log"], help="调试模式：可选 console 或 log")
    extra_group.add_argument("--note", type=int, choices=[1, 2], nargs="?", const=1, default=0,
                             help="启用笔记模式：1级输出提取内容与冲突警告，2级额外报告向AI提供的术语库")
    extra_group.add_argument("--output", action="store_true", help="输出 AI 的思考过程到控制台")
    extra_group.add_argument("--keep-asterisk", action="store_true", help="保留译文中的星号（默认删除，以防 Markdown 残留）")
    extra_group.add_argument("--no-json-mode", dest="json_mode", action="store_false",
                             help="不发送 response_format=json_object（部分推理模型不支持）")

    robust_group = parser.add_argument_group("稳定性 (Reliability)")
    robust_group.add_argument("--retries", type=int, default=3, metavar="N", help="单次请求失败后的最大重试次数")
    robust_group.add_argument("--retry-delay", type=float, default=1.0, metavar="SEC", help="重试的初始退避时间（指数增长）")
    robust_group.add_argument("--timeout", type=float, default=300.0, metavar="SEC", help="单次请求超时时间")
    robust_group.add_argument("--force", action="store_true", help="即使已存在 _translated.txt 也重新翻译")
    robust_group.add_argument("--fail-fast", action="store_true", help="任一章失败后立即停止（旧版默认行为）")
    robust_group.add_argument("--dry-run", action="store_true", help="只组装提示词并打印，不调用 API、不写文件")
    robust_group.add_argument("--reindex", action="store_true", help="仅重建 global_glossary_tracker.json 后退出")
    robust_group.add_argument("--log-dir", default="log", metavar="PATH", help="--debug log 的日志目录")

    inject_group = parser.add_argument_group("提示词注入 (Prompt injection)")
    inject_group.add_argument("--instruction", default="", metavar="TEXT",
                              help="本次任务的补充要求，会以 <user_supplement> 注入提示词")
    inject_group.add_argument("--instruction-file", default=None, metavar="PATH",
                              help="从文件读取补充要求（面板用这种方式传长文本，避免命令行转义问题）")

    inject_group.add_argument("--rebuild-glossary", action="store_true",
                              help="忽略本章已有术语库，按本次译文整体重建（默认是合并写入）")

    glossary_group = parser.add_argument_group("术语库一致性 (Glossary)")
    glossary_group.add_argument(
        "--allow-term-changes", action="store_true",
        help="允许本次翻译覆盖既定译法（默认严格先到先得；全局 glossary.json 永不可覆盖）",
    )
    glossary_group.add_argument(
        "--show-conflicts", action="store_true",
        help="打印 glossary_conflicts.jsonl 中累计的冲突记录后退出",
    )

    rag_group = parser.add_argument_group("RAG 术语上下文检索 (Retrieval)")
    rag_group.add_argument(
        "--rag", action="store_true",
        help="开启检索：为可能有特定内涵的词检索全书（默认后文）用法语境，辅助选定译法",
    )
    rag_group.add_argument("--rag-terms", type=int, default=10, metavar="N", help="每次最多检索多少个候选词")
    rag_group.add_argument("--rag-snippets", type=int, default=3, metavar="K",
                           help="每个候选词最多取几条上下文片段（优先分散在不同章节）")
    rag_group.add_argument("--rag-window", type=int, default=100, metavar="W", help="片段在命中位置两侧扩展的字符数")
    rag_group.add_argument("--rag-scope", choices=["future", "past", "all"], default="future",
                           help="检索范围：future=只看后文（默认）, past=只看前文, all=全后文优先")
    rag_group.add_argument("--rag-budget", type=int, default=9000, metavar="CHARS",
                           help="RAG 结果注入提示词的字符预算上限（按词条均摊，不会整块丢弃后面的词）")
    rag_group.add_argument("--no-rag-ask", dest="rag_ask", action="store_false",
                           help="RAG 开启时默认会额外问一次模型「你觉得哪些词要查」；加此项可只跑启发式候选")

    parser.set_defaults(json_mode=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        translator = Translator(args)
    except SystemExit as exc:
        ui.error(str(exc.code) if exc.code else "初始化失败")
        return EXIT_FATAL
    except llm.DependencyError as exc:
        ui.error(str(exc))
        return EXIT_DEPENDENCY

    if args.show_conflicts:
        records = translator.store.read_conflicts()
        if not records:
            ui.info("没有累计的术语冲突记录。")
        else:
            ignored = [r for r in records if r.get("action") == "ignored"]
            overridden = [r for r in records if r.get("action") == "overridden"]
            ui.info(
                f"共 {len(records)} 条冲突记录：已拦截 {len(ignored)} 条、已覆盖 {len(overridden)} 条"
            )
            for record in records:
                where = f"/{record.get('context')}" if record.get("context") else ""
                origin = "全局" if record.get("origin") == "global" else f"ch{record.get('origin_chapter')}"
                print(
                    f"  [{record.get('action')}] {record.get('category')} "
                    f"{record.get('term')}{where}: {record.get('old')!r} <- {record.get('new')!r} "
                    f"({origin}, {record.get('severity')})"
                )
        translator.debug.close()
        return EXIT_OK

    if args.reindex:
        try:
            path = translator.store.write_tracker()
        except Exception as exc:  # noqa: BLE001
            ui.error(f"重建 tracker 失败: {exc}")
            translator.debug.close()
            return EXIT_FATAL
        ui.ok(f"已重建全局术语 tracker: {path}")
        translator.debug.close()
        return EXIT_OK

    if not args.dry_run:
        try:
            translator.client = llm.create_client(
                api_key=args.api_key, base_url=args.base_url, timeout=args.timeout
            )
        except llm.DependencyError as exc:
            ui.error(str(exc))
            return EXIT_DEPENDENCY
        except Exception as exc:  # noqa: BLE001
            ui.error(str(exc))
            return EXIT_FATAL

    return translator.run()


if __name__ == "__main__":
    raise SystemExit(main())
