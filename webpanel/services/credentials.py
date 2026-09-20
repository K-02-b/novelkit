"""环境配置：读取/写入 .env 中的模型 API 配置。

安全约定
--------
* 接口**从不返回**任何密钥值，只返回"是否已配置"、长度等元信息。
* 写 .env 采用**逐行替换**：只改动目标键那一行，其它行（注释、空行、别的键）
  原样保留，避免"解析再渲染"把用户的 .env 改坏。
* 覆盖前先备份为 .env.bak；写好后把权限设回 0600。
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = _PROJECT_ROOT / ".env"

API_KEY = "API_KEY"
API_BASE_URL = "API_BASE_URL"
API_MODEL = "API_MODEL"

REQUIRED_KEYS: Dict[str, str] = {
    API_KEY: "翻译模型 API Key",
}

# 可选项：留空就用内置默认，因此**不影响** all_set 的判定。
# 面板「模型配置」写这三个键，命令行与面板读同一份 .env。
OPTIONAL_KEYS: Dict[str, str] = {
    API_BASE_URL: "模型 API 地址（留空则用内置默认）",
    API_MODEL: "模型 ID（留空则用内置默认）",
}

# 这些键的值**可以公开回显**（不是密钥）：面板要把当前设置显示出来供修改。
PLAINTEXT_KEYS = {API_BASE_URL, API_MODEL}


def _optional_default(key: str) -> str:
    from novelkit import config as nkconfig

    return nkconfig.DEFAULT_BASE_URL if key == API_BASE_URL else nkconfig.DEFAULT_MODEL


def _read_env() -> str:
    try:
        return ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _env_value(text: str, key: str) -> str:
    # 注意用 [ \t] 而不是 \s：\s 会匹配换行，导致**值为空时把下一行吞进来**
    # （`API_BASE_URL=` 后面若紧跟 `API_MODEL=`，旧正则会把 "API_MODEL=" 当成前者的值）。
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=[ \t]*(.*)$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def status() -> Dict[str, Any]:
    """只返回"有没有配置"，绝不返回值本身（可公开项除外）。"""
    text = _read_env()
    info: Dict[str, Any] = {}
    for key, description in REQUIRED_KEYS.items():
        raw = _env_value(text, key) or os.environ.get(key, "")
        info[key] = {
            "configured": bool(raw),
            "length": len(raw),
            "prefix": (raw[:6] + "…") if raw else "",
            "description": description,
        }

    # 可选项：base_url / model 不是密钥，直接把当前值回显，方便在面板里改。
    for key, description in OPTIONAL_KEYS.items():
        raw = (_env_value(text, key) or os.environ.get(key, "") or "").strip()
        info[key] = {
            "configured": bool(raw),
            "length": len(raw),
            "value": raw,
            "default": _optional_default(key),
            "description": description,
            "optional": True,
        }

    env_exists = ENV_PATH.exists()
    mtime = ""
    if env_exists:
        try:
            mtime = datetime.datetime.fromtimestamp(
                ENV_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            pass

    return {
        "ok": True,
        "env_path": os.path.relpath(ENV_PATH, _PROJECT_ROOT),
        "env_exists": env_exists,
        "env_mtime": mtime,
        "keys": info,
        # 只统计**必填**项：新增的可选项（地址/模型）留空时用内置默认，
        # 不应该把"全部已配置"拖成 false。
        "all_set": all(info[key]["configured"] for key in REQUIRED_KEYS),
    }


def save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把凭据写入 .env。只更新显式提供的键，其余行原样保留。"""
    updates: Dict[str, str] = {}

    value = payload.get(API_KEY)
    if isinstance(value, str) and value.strip():
        updates[API_KEY] = value.strip()

    # 可选项：允许**显式清空**（留空 = 回到内置默认），
    # 所以这里用 "键存在" 判断，而不是 "值非空"。
    for key in OPTIONAL_KEYS:
        if key in payload and isinstance(payload.get(key), str):
            updates[key] = payload[key].strip()

    if not updates:
        return {"ok": False, "error": "没有提供任何要保存的内容"}

    # 常见粘贴错误：把 "API_KEY=sk-xxx" 整行粘进了输入框
    api_key = updates.get(API_KEY, "")
    if api_key.startswith(f"{API_KEY}="):
        updates[API_KEY] = api_key.split("=", 1)[1].strip()

    # 必填项不能清空；可选项允许清空（表示"恢复默认"）
    stale = [key for key, value in updates.items()
             if not value and key not in OPTIONAL_KEYS]
    for key in stale:
        updates.pop(key, None)
    if not updates:
        return {"ok": False, "error": "提供的值为空"}

    # dry-run：只解析并报告会改动哪些键，绝不碰磁盘。
    # 这样测试/预览凭据解析时不会意外覆盖真实 .env。
    if payload.get("dry_run"):
        return {
            "ok": True,
            "dry_run": True,
            "would_update": sorted(updates.keys()),
            "recognized": {k: f"{len(v)} 字符 · {v[:6]}…" for k, v in updates.items()},
            "env_path": os.path.relpath(ENV_PATH, _PROJECT_ROOT),
        }

    original = _read_env()
    written: List[str] = []
    lines = original.splitlines()
    for index, line in enumerate(lines):
        for key, value in updates.items():
            if re.match(rf"^[ \t]*{re.escape(key)}[ \t]*=", line):
                lines[index] = f"{key}={value}"
                written.append(key)
                break

    remaining = {k: v for k, v in updates.items() if k not in written}
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if remaining:
        if text and not text.endswith("\n\n"):
            text += "\n"
        for key, value in remaining.items():
            text += f"{key}={value}\n"

    # 注意：Path(".env").with_suffix(".env.bak") 会得到 ".env.env.bak"，
    # 正确写法是直接接在文件名后面，与项目里已有的 .env.bak 约定一致。
    backup_path = ENV_PATH.with_name(ENV_PATH.name + ".bak")
    backup = ""
    if ENV_PATH.exists():
        try:
            shutil.copy2(ENV_PATH, backup_path)
            backup = os.path.relpath(backup_path, _PROJECT_ROOT)
        except OSError:
            pass

    ENV_PATH.write_text(text, encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass

    # 让面板进程内立即生效（命令行脚本每次都重新读 .env）
    for key, value in updates.items():
        os.environ[key] = value

    return {
        "ok": True,
        "updated": sorted(updates.keys()),
        "appended": sorted(remaining.keys()),
        "backup": backup,
        "env_path": os.path.relpath(ENV_PATH, _PROJECT_ROOT),
        "status": status(),
    }


def verify_model_api() -> Dict[str, Any]:
    """用当前配置向模型服务发一次最小请求，判断 API Key / 地址是否可用。"""
    from novelkit import config as nkconfig
    from novelkit import llm as nkllm

    api_key = nkconfig.get_api_key(required=False)
    if not api_key:
        return {"ok": False, "error": ".env 里还没有配置 API_KEY"}

    base_url = nkconfig.get_base_url()
    model = nkconfig.get_model()

    try:
        client = nkllm.create_client(api_key=api_key, base_url=base_url, timeout=30.0)
    except nkllm.DependencyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        # 只发一个最小请求：能列出模型信息就说明地址与 Key 都能用。
        client.models.list()
    except Exception as exc:  # noqa: BLE001 — 网络/鉴权/额度错误都归一成一句提示
        return {"ok": False, "error": f"调用失败：{type(exc).__name__}: {exc}"}

    return {"ok": True, "message": f"模型服务可达（{model}）", "model": model,
            "base_url": base_url}
