"""配置：.env 加载、API Key、路径常量。

设计要点：
1. API Key 只在"真正要发请求"时才校验，因此 `python scripts/translate.py --help`
   在没有 .env 的情况下也能正常工作（旧版在 import 阶段就 raise，导致连帮助都看不了）。
2. python-dotenv 缺失时退化为内置的极简 .env 解析器，少一个硬依赖。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 目录布局
#   项目根/            代码仓库（不要把作品放进这里）
#   ├── config/        全局配置：默认提示词、全局术语库
#   ├── scripts/       命令行入口
#   ├── works/         作品工作区：一部作品一个子目录（导入的作品都放这里）
#   ├── webpanel/      Web 面板
#   └── docs/ tests/ deploy/
# 作品工作区可以用 NOVELKIT_WORKSPACE 环境变量指到别处（例如数据盘）。
# ---------------------------------------------------------------------------
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PROMPT_PATH = CONFIG_DIR / "prompt.txt"
GLOBAL_GLOSSARY_PATH = CONFIG_DIR / "glossary.json"
WORKSPACE_DIRNAME = "works"
WORK_PROMPT_FILENAME = "prompt.txt"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# 默认走 DeepSeek 官方接口。任何 OpenAI 兼容服务都可以用：
# 在 .env 里设置 API_BASE_URL / API_MODEL 即可覆盖这两个内置默认值。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# .env 里的键名：面板「模型配置」写这三个，命令行与面板都读同一份配置
ENV_API_KEY = "API_KEY"
ENV_BASE_URL = "API_BASE_URL"
ENV_MODEL = "API_MODEL"

_ENV_LOADED = False


def _parse_env_file(path: Path) -> dict:
    """极简 .env 解析器：KEY=VALUE，# 开头为注释，支持单双引号包裹。"""
    values: dict = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(env_path: Optional[os.PathLike | str] = None) -> None:
    """加载 .env。已存在的环境变量优先（不覆盖），避免误伤 CI/容器注入。"""
    global _ENV_LOADED

    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    else:
        candidates.append(PROJECT_ROOT / ".env")
        candidates.append(Path.cwd() / ".env")

    try:
        from dotenv import load_dotenv  # type: ignore

        for path in candidates:
            if path.exists():
                load_dotenv(dotenv_path=str(path), override=False)
    except ImportError:
        for path in candidates:
            if not path.exists():
                continue
            for key, value in _parse_env_file(path).items():
                os.environ.setdefault(key, value)

    _ENV_LOADED = True


def get_api_key(required: bool = True) -> Optional[str]:
    """读取 API Key。required=True 且缺失时抛出带指引的异常。"""
    if not _ENV_LOADED:
        load_env()
    key = (os.environ.get("API_KEY") or "").strip()
    if key:
        return key
    if required:
        raise RuntimeError(
            f"未找到 API_KEY 环境变量。请在 {PROJECT_ROOT / '.env'} 中写入：\n"
            "    API_KEY=sk-xxxxxxxx\n"
            "或先执行 export API_KEY=sk-xxxxxxxx"
        )
    return None


def get_base_url(default: Optional[str] = None) -> str:
    """API 地址：.env 的 API_BASE_URL 优先，其次内置默认。"""
    return (get_env(ENV_BASE_URL) or "").strip() or (default or DEFAULT_BASE_URL)


def get_model(default: Optional[str] = None) -> str:
    """模型 ID：.env 的 API_MODEL 优先，其次内置默认。"""
    return (get_env(ENV_MODEL) or "").strip() or (default or DEFAULT_MODEL)


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    if not _ENV_LOADED:
        load_env()
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def workspace_root() -> Path:
    """作品工作区根目录：默认 `<项目根>/works/`，可用 NOVELKIT_WORKSPACE 覆盖。

    也可以把 NOVELKIT_WORKSPACE 写进 `.env`（与 API 配置同一份文件）。
    """
    if not _ENV_LOADED:
        load_env()
    raw = (os.environ.get("NOVELKIT_WORKSPACE") or "").strip()
    if raw:
        base = Path(raw).expanduser()
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        return base.resolve()
    return (PROJECT_ROOT / WORKSPACE_DIRNAME).resolve()


def resolve_work_dir(path: str) -> Path:
    """把 --dir 解析成绝对路径。

    绝对路径原样使用；相对路径优先在工作区（works/）里找，
    找不到再退回按当前工作目录解析，这样旧的 `--dir ./somewhere` 也不会失效。
    """
    candidate = Path(str(path)).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    in_workspace = workspace_root() / candidate
    if in_workspace.exists():
        return in_workspace.resolve()
    if (Path.cwd() / candidate).exists():
        return (Path.cwd() / candidate).resolve()
    return in_workspace.resolve()
