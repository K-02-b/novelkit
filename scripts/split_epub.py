#!/usr/bin/env python3
"""把 EPUB 按固定规则拆成一章一个 `<章号>_origin.txt`。

这是面板「导入 EPUB」的**命令行简化版**：右下角面板可以逐条预览、勾选、
指定哪一条是简介，功能更全；这里只做一次性的全量拆分，适合脚本化批处理。

规则（与面板导入的默认行为一致）：
  * 第 2 个文档条目 → `0_origin.txt`（简介）；
  * 从第 4 个文档条目起 → `1_origin.txt`、`2_origin.txt` …（正文）。

用法：
    python scripts/split_epub.py book.epub
    python scripts/split_epub.py book.epub -o works/my-novel
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelkit import config  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEPENDENCY = 3


def extract_chapters(epub_path: str, output_dir: str) -> int:
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - 取决于本机环境
        print(f"缺少依赖 ebooklib / beautifulsoup4：{exc}", file=sys.stderr)
        print("请先执行：.venv/bin/pip install -r requirements.txt", file=sys.stderr)
        return EXIT_DEPENDENCY

    if not os.path.exists(epub_path):
        print(f"错误: 找不到文件 '{epub_path}'", file=sys.stderr)
        return EXIT_ERROR

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"创建目录: {output_dir}")

    try:
        book = epub.read_epub(epub_path)
        items = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]

        if len(items) < 4:
            print("错误: EPUB 文件结构异常，文件总数不足以执行排除操作。", file=sys.stderr)
            return EXIT_ERROR

        # 1. 第二个文档条目作为简介（索引为 1）
        intro_text = BeautifulSoup(items[1].get_content(), "html.parser").get_text().strip()
        Path(output_dir, "0_origin.txt").write_text(intro_text, encoding="utf-8")
        print("已保存简介: 0_origin.txt")

        # 2. 正文从第四个文档条目开始（索引为 3）
        for i, item in enumerate(items[3:], start=1):
            try:
                text = BeautifulSoup(item.get_content(), "html.parser").get_text().strip()
            except Exception as exc:  # noqa: BLE001
                print(f"处理第 {i + 3} 个文件时出错: {exc}", file=sys.stderr)
                continue
            if not text:
                continue
            name = f"{i}_origin.txt"
            Path(output_dir, name).write_text(text, encoding="utf-8")
            print(f"成功导出正文: {name}")

    except Exception as exc:  # noqa: BLE001
        print(f"读取 EPUB 文件时发生致命错误: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 EPUB 拆成一章一个 txt（面板「导入 EPUB」的简化版）。")
    parser.add_argument("input", help="输入的 EPUB 文件路径")
    parser.add_argument("-o", "--output", default=None,
                        help="输出目录（默认 works/<EPUB 文件名>）")
    args = parser.parse_args(argv)

    output = args.output or str(config.workspace_root() / Path(args.input).stem)
    return extract_chapters(args.input, output)


if __name__ == "__main__":
    raise SystemExit(main())
