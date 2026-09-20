# 命令行流水线

面板覆盖了日常使用（翻译、局部重译、精修、术语维护、校对）。这篇文档写给需要
**批量跑、写脚本、或在没有浏览器的机器上工作**的人：面板底层的脚本可以直接调用，
参数与面板里的操作一一对应。

> 所有命令都假设已经装好依赖、写好 `.env`（见根目录 README 的「安装与启动」）。
> 从**项目根目录**执行，解释器统一用 `.venv/bin/python`（下文简写为 `python`）。

## 0. 工作区与 `--dir`

导入的作品统一放在 **`works/`**（作品工作区，不进版本库）。一部作品 = 一个子目录：

```
works/
  my-novel/       ← --dir my-novel
  另一部作品/
```

`--dir` 的解析规则：

* 相对名字（`--dir my-novel`）优先到 `works/` 下查找，找不到再按当前目录解析；
* 也可以直接给绝对路径（`--dir /data/novels/my-novel`）；
* 想把作品放到别的盘，用环境变量把整个工作区挪走：

```bash
export NOVELKIT_WORKSPACE=/data/novels
```

## 1. 整章翻译 — `scripts/translate.py`

```bash
# 把 my-novel 里所有尚未翻译的章节翻完
python scripts/translate.py --dir my-novel --tasks 9999

# 指定章节：空格、逗号、区间、中文括号都支持
python scripts/translate.py --dir my-novel --chapter "2-3"
python scripts/translate.py --dir my-novel --chapter "10,15,17,19,39,41,51"
python scripts/translate.py --dir my-novel --chapter "1-4 6 8-9"

# 翻译 + 回译 + AI 评审
python scripts/translate.py --dir my-novel --chapter 12 --back --review

# 开启 RAG 术语检索，并加大前后文参考
python scripts/translate.py --dir my-novel --chapter 12 --rag --context 800 --future 400

# 离线演练：只组装提示词并打印，不调用 API、不写文件
python scripts/translate.py --dir my-novel --chapter "1-2" --dry-run
```

常用参数（完整列表见 `python scripts/translate.py --help`）：

| 参数 | 作用 |
| --- | --- |
| `--dir` | 作品目录（必填；相对名字优先在 `works/` 下查找） |
| `--chapter` / `--tasks` | 指定章节，或按数量连续翻译（二者互斥） |
| `--model` / `--base_url` / `--api_key` | 覆盖 `.env` 里的模型配置 |
| `--model_back` / `--model_review` | 回译 / 评审单独指定模型 |
| `--context` / `--future` | 前文 / 后文参考量（正数=长度，负数=章节数） |
| `--unit` | `context`/`future` 正数的计量单位（auto / words / chars） |
| `--anchor` | 风格基准强度：0 不参考 / 1 仅简介 / 2 简介+首章采样 |
| `--temp` `--top_p` `--presence_penalty` `--repetition_penalty` | 采样与惩罚参数 |
| `--thinking` `--thinking_budget` | 启用模型思考过程并给预算 |
| `--zh` `--zh-retry` | 检查中文残留，并可自动重译 N 次 |
| `--back` `--review` `--refine` | 翻译后追加回译 / 评审 / 评审后重译 |
| `--dry-run` | 只组装提示词并打印，不调用 API、不写文件 |
| `--force` | 已有译文也重新翻译 |
| `--fail-fast` | 任一章失败后立即停止 |
| `--retries` `--retry-delay` `--timeout` | 重试次数、退避初值、单次请求超时 |
| `--instruction` / `--instruction-file` | 本次任务的补充要求（注入 `<user_supplement>`） |
| `--rebuild-glossary` | 忽略本章已有术语库，按本次译文整体重建 |
| `--allow-term-changes` | 允许覆盖既定译法（全局术语库始终不可覆盖） |
| `--show-conflicts` | 打印累计的冲突记录后退出 |
| `--reindex` | 只重建 `global_glossary_tracker.json` 后退出 |
| `--rag` `--rag-scope` `--rag-terms` `--rag-snippets` `--rag-window` `--rag-budget` | RAG 术语上下文检索 |
| `--debug [console\|log]` `--log-dir` `--note [1\|2]` `--output` | 日志与调试输出 |

## 2. 校对 — `scripts/check.py`

```bash
# 中文残留 / Emoji 检测
python scripts/check.py --dir my-novel --chapter "1-5" --zh --emoji

# 回译 + AI 评审（可调并发）
python scripts/check.py --dir my-novel --chapter "1-5" --back --review --jobs 4

# 覆盖已存在的回译 / 评审文件
python scripts/check.py --dir my-novel --chapter "1-5" --back --review --force
```

`--chapter` 默认 `all`；也可以在面板「中英对照」里逐章做。

## 3. 局部重译 — `scripts/retranslate.py`

只重译选中的段落，上下文、术语库与 RAG 检索与整章翻译完全一致，
未选中的段落逐字节保持原样。

```bash
# 行号来自对齐结果（面板里勾选段落即可看到行号）
python scripts/retranslate.py --dir my-novel --chapter 12 --rows "3,7-9"

# 带 RAG，并允许覆盖章节级既定译法
python scripts/retranslate.py --dir my-novel --chapter 12 --rows "3" --rag --allow-term-changes
```

## 4. 精修 — `scripts/refine.py`

逐段点评并修订译文，每处改动都留下**理由**（写入 `<章号>_refine.json`）：

```bash
# 全章
python scripts/refine.py --dir my-novel --chapter 12

# 只处理指定段落
python scripts/refine.py --dir my-novel --chapter 12 --rows "3,7-9"

# 不参考现有译文（等价于重译，但同样给出理由）
python scripts/refine.py --dir my-novel --chapter 12 --ignore-draft
```

面板「中英对照」会把仍然有效的理由挂在对应段落上——如果译文后来又被改过，
旧理由会自动隐藏，避免误导。

## 5. 仅拆分 EPUB — `scripts/split_epub.py`

面板自带「导入 EPUB」，可以逐条预览、勾选、指定简介条目，通常不需要这个脚本。
只有在没有浏览器、又要一次性全量拆分时才用它：

```bash
# 默认输出到 works/<EPUB 文件名>/
python scripts/split_epub.py book.epub

# 指定输出目录
python scripts/split_epub.py book.epub -o works/my-novel
```

注意它在 EPUB 内部按固定下标取正文（简介=第 2 个文档条目、正文=其后全部），
不同 EPUB 的封面 / 版权页 / 目录页数量不一样，可能会错位——错位时请改用面板导入。

## 6. 文件约定

一部作品就是一个目录，里面按章存放纯文本（一行一段）：

```
works/my-novel/
  0_origin.txt              简介（可选）
  1_origin.txt              第 1 章中文原文
  1_translated.txt          第 1 章英文译文
  1_back.txt                回译（--back）
  1_review.txt              AI 评审（--review）
  1_refine.json             精修理由（refine.py）
  glossary_1.json           本章术语
  global_glossary_tracker.json   全书术语总表
  glossary_conflicts.jsonl  术语冲突流水
  prompt.txt                这部作品专用的提示词覆盖（可选，兼容旧的 提示词.txt）
  log/                      调试日志（--debug log）
```

跨作品共用的全局术语库与默认提示词放在 `config/`：

```
config/
  glossary.json             全局术语库
  prompt.txt                默认翻译提示词模板
```

`.backups/` 是面板「编辑」写文件前留下的历史版本，命令行不产生它。

## 7. 想省事就用面板

上面每个操作在面板里都有对应入口，而且不必记参数：

| 命令行 | 面板位置 |
| --- | --- |
| `scripts/translate.py` | 作品的「中英对照」→ 本章操作 → 翻译本章 / 重新翻译本章 |
| `scripts/retranslate.py` | 同上 → 勾选「分段选择」→ 处理选中的 N 段 |
| `scripts/refine.py` | 同上 → 勾选「忽略现有译文」与分段选择 |
| `scripts/check.py` | 面板暂无入口，用命令行 |
| `scripts/split_epub.py` | 全局 → 导入 EPUB |

术语的手工增删改（`glossary_*.json` / 作品级 `glossary.json` / 全局 `config/glossary.json`）
在「术语库」页做更省事，带实时查重与冲突提示。
