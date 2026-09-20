"""外部工具：在侧栏提供入口，并由面板**反向代理**到外部 WebUI（默认作品导出工具）。

设计取舍
--------
* **本项目不附带/不内嵌该下载器**（第三方软件，MIT 许可，与本项目仅通过 HTTP 集成）。
  安装方式见 README 的部署章节，源码：https://github.com/zhongbai2333/Tomato-Novel-Downloader
* **反代的意义**：云服务器上面板监听 `8897`，而下载器只监听服务器自己的
  `127.0.0.1:18423`。如果让浏览器直连 `127.0.0.1:18423`，那指的是**用户自己的电脑**，
  必然打不开。所以侧栏入口走面板同源的 `/tools/<id>/`，由服务端转发到
  `tools.json` 里配置的地址——不用对外开放下载器端口，也不用额外配 Nginx location。
* **前缀改写**：这类 WebUI 普遍使用根绝对路径（`/assets/app.js`、`/api/status`），
  挂到子路径下会 404。因此对 HTML/CSS/JS/JSON 响应体做**按前缀改写**
  （见每个工具的 `proxy_prefixes`），并同步改写 `Location` 与 `Set-Cookie` 的 Path。
* 地址可配置（写入 webpanel/tools.json），因为下载器端口会被 `TOMATO_WEB_ADDR` 改掉。
* 可达性用**服务端 TCP 探测**，不用浏览器 fetch：跨端口 fetch 会被 CORS 拦掉，
  而且探测结果能顺便告诉用户"服务没开"，比打开后白屏友好。
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_PATH = _PROJECT_ROOT / "webpanel" / "tools.json"

DEFAULTS: List[Dict[str, Any]] = [
    {
        "id": "tomato",
        # 名字用中性的"作品导出工具"：它是通用工具，不点名具体第三方项目
        "label": "作品导出工具",
        "url": "http://localhost:18423/",
        # 说明只留在代码里：界面不再展示长提示（侧栏入口只显示名称与可达性圆点）
        "hint": "第三方工具，本项目不附带。只应用于你自己拥有版权或已获授权的作品。",
        # 该 WebUI 用根绝对路径，反代到子路径下必须改写这些前缀才不会 404。
        "proxy_prefixes": ["/api/", "/assets/", "/download/", "/download-zip/"],
    },
]


def _load_overrides() -> Dict[str, str]:
    try:
        with TOOLS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError):
        pass
    return {}


def _save_overrides(overrides: Dict[str, str]) -> Path:
    TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".tools_", dir=str(TOOLS_PATH.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, TOOLS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return TOOLS_PATH


_HOST_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def validate_url(url: str) -> tuple:
    """校验跳转地址。返回 (规范化后的地址, 错误信息)。"""
    url = (url or "").strip()
    if not url:
        return "", ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "", "只支持 http / https 地址"
        host = parsed.hostname
        if not host:
            return "", "缺少主机名"
        if not _HOST_RE.match(host):
            return "", "主机名含有非法字符"
        port = parsed.port          # 端口非法时这里会抛 ValueError
        if port is not None and not (1 <= port <= 65535):
            return "", "端口需在 1-65535 之间"
    except ValueError as exc:
        return "", f"地址无法解析：{exc}"
    return url, ""


def probe(url: str, *, timeout: float = 0.35) -> bool:
    """TCP 探测主机:端口是否可连（比发 HTTP 请求快，也不受 CORS 限制）。

    允许传裸的 host:port —— 与 validate_url 的宽松处理保持一致，
    否则调用方得先自己补 schema，容易漏。
    """
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def list_tools() -> List[Dict[str, Any]]:
    overrides = _load_overrides()
    result: List[Dict[str, Any]] = []
    for item in DEFAULTS:
        url = overrides.get(item["id"], item["url"])
        result.append({
            "id": item["id"],
            "label": item["label"],
            "url": url,
            "proxy_prefixes": list(item.get("proxy_prefixes") or []),
            "customised": item["id"] in overrides,
            "reachable": probe(url),
        })
    return result


def get(tool_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取**生效后**的工具配置（含 url 与 proxy_prefixes），供反代使用。"""
    for item in list_tools():
        if item["id"] == tool_id:
            return item
    return None


def set_url(tool_id: str, url: str) -> Dict[str, Any]:
    known = {item["id"] for item in DEFAULTS}
    if tool_id not in known:
        return {"ok": False, "error": f"未知工具: {tool_id}"}
    url, error = validate_url(url)
    if error:
        return {"ok": False, "error": error}

    overrides = _load_overrides()
    default_url = next(item["url"] for item in DEFAULTS if item["id"] == tool_id)
    if not url or url == default_url:
        overrides.pop(tool_id, None)   # 恢复默认
    else:
        overrides[tool_id] = url
    path = _save_overrides(overrides)
    try:
        shown = str(path.relative_to(_PROJECT_ROOT))
    except ValueError:      # 配置文件不在项目内（测试/自定义路径）时不要崩
        shown = str(path)
    return {"ok": True, "path": shown, "tools": list_tools()}
