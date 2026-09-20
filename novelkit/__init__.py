"""novelkit — 网文翻译工具链的共享基础库。

模块划分：
    text     章节号解析、标点规范化、中文/Emoji 检测、原子写入
    config   .env / API Key / 路径常量
    llm      OpenAI 兼容客户端、带退避的重试、健壮的 JSON 抽取
    glossary 术语库读写、权威译法登记（先到先得）、冲突拦截、tracker
    rag      术语上下文检索（候选词发现 + 全书语境片段）
    align    Gale-Church 中英段落对齐
    prompt   提示词组装（整章翻译与局部重译共用）
    ui       终端配色、日志与调试输出
"""

from . import align, config, glossary, llm, prompt, rag, text, ui  # noqa: F401

__all__ = ["text", "config", "llm", "glossary", "rag", "align", "prompt", "ui"]
__version__ = "2.2.0"
