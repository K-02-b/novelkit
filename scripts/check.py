#!/usr/bin/env python3
"""译文检查工具：中文/Emoji 检测、回译、AI 评审。

用法示例（与旧版完全兼容）：
    python checker.py --dir air --zh
    python checker.py --dir air --back --review --chapter 1,3,5-10
    python checker.py --dir air --back --chapter all --jobs 4

相对旧版的主要改进
------------------
  * 有界并发：旧版 `asyncio.gather(*tasks)` 会把 766 章一次性全发出去，
    必然触发限流与 "Connection error."；现在用信号量限制在 --jobs（默认 4）以内。
  * 回译/评审提示词改用正确的 system+user 角色（旧版把两条都写成 user）。
  * 每个请求带指数退避重试，并显式设置超时。
  * 已存在的 _back.txt / _review.txt 默认跳过（可断点续跑），--force 覆盖。
  * 章节筛选统一走 novelkit.text.parse_chapters：空格/逗号/区间/【】/all 全支持。
  * --zh 报告中文与 Emoji 的字符数与行号，便于定位。
  * 结束时汇总成功/失败，并以退出码反映结果。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelkit import config, llm  # noqa: E402
from novelkit import text as nktext  # noqa: E402
from novelkit import ui  # noqa: E402

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
EXIT_DEPENDENCY = 3

BACK_SYSTEM_PROMPT = "你是一个翻译助手，将英文小说翻译回中文，保持原有换行结构，只输出译文本身。"
REVIEW_SYSTEM_PROMPT = "你是一个专业的翻译评审专家，尤其需要找出不足以帮助改进。"


# --------------------------------------------------------------------------
# 客户端（保持旧版可 import 的接口）
# --------------------------------------------------------------------------

def get_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    timeout: float = 300.0,
    max_retries: int = 0,
):
    """创建异步客户端。旧版调用 get_client() 依然可用。"""
    return llm.create_async_client(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
    )


def parse_chapters(chapter_str: Optional[str]):
    """旧版接口保留：'all'/空 → None（不筛选）。"""
    return nktext.parse_chapters(chapter_str, allow_all=True)


# --------------------------------------------------------------------------
# 单条检查
# --------------------------------------------------------------------------

async def run_back(
    client,
    content: str,
    model: str,
    output_path: str,
    *,
    retries: int = 3,
    retry_delay: float = 1.0,
    force: bool = False,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> bool:
    """把英文译文回译成中文，写入 output_path。"""
    if not force and os.path.exists(output_path):
        ui.info(f"回译已存在，跳过: {output_path}")
        return True

    async def attempt():
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": BACK_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        text = response.choices[0].message.content or ""
        if not text.strip():
            raise llm.ResponseParseError("回译返回内容为空")
        return text

    async def guarded():
        if semaphore is None:
            return await attempt()
        async with semaphore:
            return await attempt()

    try:
        text = await llm.arun_with_retry(guarded, retries=retries, base_delay=retry_delay)
        nktext.write_text_atomic(output_path, text)
        ui.ok(f"回译完成: {output_path}")
        return True
    except Exception as exc:  # noqa: BLE001
        ui.error(f"回译失败 {output_path}: {_short(exc)}")
        return False


async def run_review(
    client,
    origin: str,
    translated: str,
    glossary: str,
    model: str,
    output_path: str,
    *,
    retries: int = 3,
    retry_delay: float = 1.0,
    force: bool = False,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> bool:
    """对译文做 AI 评审，写入 output_path。"""
    if not force and os.path.exists(output_path):
        ui.info(f"评审已存在，跳过: {output_path}")
        return True

    prompt = f"请评价以下翻译的质量。\n原文:\n{origin}\n\n译文:\n{translated}\n\n术语表:\n{glossary}"

    async def attempt():
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content or ""
        if not text.strip():
            raise llm.ResponseParseError("评审返回内容为空")
        return text

    async def guarded():
        if semaphore is None:
            return await attempt()
        async with semaphore:
            return await attempt()

    try:
        text = await llm.arun_with_retry(guarded, retries=retries, base_delay=retry_delay)
        nktext.write_text_atomic(output_path, text)
        ui.ok(f"评审完成: {output_path}")
        return True
    except Exception as exc:  # noqa: BLE001
        ui.error(f"评审失败 {output_path}: {_short(exc)}")
        return False


def _short(exc: BaseException) -> str:
    lines = str(exc).splitlines()
    return lines[0][:200] if lines and lines[0] else type(exc).__name__


# --------------------------------------------------------------------------
# 中文 / Emoji 检测
# --------------------------------------------------------------------------

def check_chinese(work_dir: Path, chapters: Sequence[int], *, check_emoji: bool = False) -> int:
    """扫描译文中的中文与 Emoji。返回存在问题的章节数。"""
    problems = 0
    for number in chapters:
        path = work_dir / f"{number}_translated.txt"
        content = nktext.read_text(str(path))
        if content is None:
            ui.warn(f"文件不存在: {path}")
            continue

        issues = nktext.detect_issues(content)
        labels: List[str] = []
        if issues["has_cjk"]:
            labels.append(
                f"中文 {issues['cjk_count']} 处 (行 {_preview(issues['cjk_lines'])})"
            )
        if check_emoji and issues["has_emoji"]:
            labels.append(
                f"Emoji {issues['emoji_count']} 处 (行 {_preview(issues['emoji_lines'])})"
            )

        if labels:
            problems += 1
            ui.warn(f"{path.name}: " + " | ".join(labels))
        else:
            ui.info(f"{path.name}: 无中文" + ("、无 Emoji" if check_emoji else ""))
    return problems


def _preview(lines: List[int], limit: int = 10) -> str:
    if not lines:
        return "无"
    if len(lines) <= limit:
        return ",".join(str(x) for x in lines)
    return ",".join(str(x) for x in lines[:limit]) + f"...(共{len(lines)}行)"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def collect_chapters(work_dir: Path, selection: Optional[Set[int]]) -> List[int]:
    """列出目录下存在的 _translated.txt 章节，按 selection 过滤。"""
    numbers = nktext.chapter_files(str(work_dir), "_translated.txt")
    if selection is None:
        return numbers
    return [num for num in numbers if num in selection]


async def run_checks(args: argparse.Namespace, chapters: Sequence[int]) -> int:
    client = get_client(api_key=args.api_key, base_url=args.base_url, timeout=args.timeout)
    semaphore = asyncio.Semaphore(max(1, args.jobs))
    work_dir = config.resolve_work_dir(args.dir)
    tasks = []

    for number in chapters:
        translated = nktext.read_text(str(work_dir / f"{number}_translated.txt"), "")
        if not translated.strip():
            ui.warn(f"第 {number} 章译文为空，跳过")
            continue

        if args.back:
            tasks.append(
                run_back(
                    client, translated, args.model,
                    str(work_dir / f"{number}_back.txt"),
                    retries=args.retries, retry_delay=args.retry_delay,
                    force=args.force, semaphore=semaphore,
                )
            )

        if args.review:
            origin = nktext.read_text(str(work_dir / f"{number}_origin.txt"), "")
            if not origin.strip():
                ui.warn(f"第 {number} 章缺少原文，评审将只依据译文")
            glossary_path = work_dir / f"glossary_{number}.json"
            glossary = nktext.read_text(str(glossary_path), "") or "{}"
            tasks.append(
                run_review(
                    client, origin, translated, glossary, args.model,
                    str(work_dir / f"{number}_review.txt"),
                    retries=args.retries, retry_delay=args.retry_delay,
                    force=args.force, semaphore=semaphore,
                )
            )

    if not tasks:
        ui.info("没有需要执行的检查任务。")
        return EXIT_OK

    ui.info(f"共 {len(tasks)} 个任务，并发上限 {args.jobs}，模型 {args.model}")
    results = await asyncio.gather(*tasks)
    failed = sum(1 for ok in results if not ok)
    ui.info(f"完成: 成功 {len(results) - failed} / 失败 {failed}")
    return EXIT_OK if failed == 0 else EXIT_PARTIAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="译文检查工具（优化版）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dir", required=True, metavar="PATH",
                        help="作品目录（相对路径优先在 works/ 工作区里查找）")
    parser.add_argument("--model", default=config.DEFAULT_MODEL, help="用于回译/评审的模型")
    parser.add_argument("--base_url", default=None, help="API 地址")
    parser.add_argument("--api_key", default=None, help="API Key（默认读取 .env）")
    parser.add_argument("--zh", action="store_true", help="检查译文中是否包含中文")
    parser.add_argument("--emoji", action="store_true", help="同时检查 Emoji / 特殊符号")
    parser.add_argument("--back", action="store_true", help="执行回译")
    parser.add_argument("--review", action="store_true", help="执行 AI 评审")
    parser.add_argument("--chapter", default="all",
                        help="章节筛选：'all' / '1,3,5-10' / '1-4 6'")
    parser.add_argument("--jobs", type=int, default=4, metavar="N", help="并发请求上限")
    parser.add_argument("--retries", type=int, default=3, metavar="N", help="单个请求的最大重试次数")
    parser.add_argument("--retry-delay", type=float, default=1.0, metavar="SEC", help="重试初始退避时间")
    parser.add_argument("--timeout", type=float, default=300.0, metavar="SEC", help="单次请求超时")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的回译/评审文件")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = config.resolve_work_dir(args.dir)

    if not work_dir.is_dir():
        ui.error(f"工作目录不存在: {work_dir}")
        return EXIT_FATAL

    try:
        selection = parse_chapters(args.chapter)
    except ValueError as exc:
        ui.error(str(exc))
        return EXIT_FATAL

    selection_set = None if selection is None else set(selection)
    chapters = collect_chapters(work_dir, selection_set)

    if not chapters:
        ui.info("未找到任何匹配的译文文件。")
        return EXIT_OK

    ui.info(f"匹配章节 {len(chapters)} 个: {nktext.format_chapter_list(chapters)}")

    problems = 0
    if args.zh or args.emoji:
        problems = check_chinese(work_dir, chapters, check_emoji=args.emoji)

    status = EXIT_OK
    if args.back or args.review:
        try:
            status = asyncio.run(run_checks(args, chapters))
        except llm.DependencyError as exc:
            ui.error(str(exc))
            return EXIT_DEPENDENCY
        except KeyboardInterrupt:
            ui.warn("已中断。")
            status = EXIT_PARTIAL

    if problems and status == EXIT_OK:
        status = EXIT_PARTIAL
    return status


if __name__ == "__main__":
    raise SystemExit(main())
