#!/usr/bin/env python3
"""网文项目 Web 面板（零第三方依赖，纯标准库）。

启动：
    .venv/bin/python webpanel/server.py                  # 默认 127.0.0.1:8897（仅本机）
    .venv/bin/python webpanel/server.py --port 9000      # 换端口
    PANEL_TOKEN=xxx .venv/bin/python webpanel/server.py --host 0.0.0.0   # 云服务器

零第三方依赖：后端只用标准库，系统 python3 就能跑起来。面板负责作品浏览、
中英对照、术语库、本地编辑与后台翻译任务；真正的翻译调用在活动任务里执行，
那里才需要 .venv 里的 openai 依赖。

安全模型
--------
* **本机模式（默认）**：只监听 127.0.0.1，并拒绝非本机来源的请求。
* **远程模式**：监听非回环地址时**必须**配置访问令牌（`--token` / `PANEL_TOKEN`），
  否则拒绝启动（fail-closed，避免把面板裸奔到公网）。
  配置令牌后，浏览器首次用 `http://<host>:<port>/?token=<令牌>` 打开即可换到
  HttpOnly Cookie，之后正常访问；未认证的请求一律 302 到 `/login`（接口返回 401）。
* 页面上永远不显示 API Key 或令牌值本身。

生产建议：再用 Nginx/Caddy 套一层 HTTPS 反向代理，见 deploy/cloud.md。
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
for path in (str(_PROJECT_ROOT), str(_HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from services import credentials as creds_service  # noqa: E402
from services import import_epub as epub_service  # noqa: E402
from services import jobs as jobs_service  # noqa: E402
from services import editor as editor_service  # noqa: E402
from services import works as works_service  # noqa: E402
from services import library  # noqa: E402
from services import tools as tools_service  # noqa: E402
from services import snippets as snippets_service  # noqa: E402
from services import glossary_edit as glossary_edit_service  # noqa: E402
from novelkit import config as nkconfig  # noqa: E402

STATIC_DIR = _HERE / "static"
MAX_BODY = 96 * 1024 * 1024  # EPUB 上传走 base64，放宽上限

# 访问令牌：为空 = 本机模式（只允许回环来源）。由 main() 根据 --token/PANEL_TOKEN 设置。
AUTH_TOKEN = ""
AUTH_COOKIE = "novelkit_token"
# 这些路径在未认证时也要能访问（登录页与登出）。
PUBLIC_PATHS = {"/login", "/logout"}


def token_ok(candidate: str) -> bool:
    """常量时间比较，避免通过响应时间猜令牌。"""
    return bool(AUTH_TOKEN) and hmac.compare_digest(candidate or "", AUTH_TOKEN)


def is_loopback_host(host: str) -> bool:
    return (host or "").strip() in ("127.0.0.1", "::1", "localhost", "")


# ---------------------------------------------------------------------------
# 外部工具反向代理（/tools/<id>/... → tools.json 里配置的地址）
# ---------------------------------------------------------------------------

# 关闭后 /tools/ 一律 403，侧栏改为直连（--no-tool-proxy / PANEL_TOOL_PROXY=0）
TOOL_PROXY_ENABLED = True
PROXY_TIMEOUT = 300
# 逐跳首部 + 由本进程重新计算的首部，不原样转发
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}
# 这些类型才做前缀改写（二进制一律原样透传）
_PROXY_TEXT_TYPES = {
    "text/html", "text/css", "text/plain", "text/javascript", "application/javascript",
    "application/json", "application/xml", "text/xml", "image/svg+xml",
}


def _proxy_prefix(tool_id: str) -> str:
    return "/tools/" + tool_id


def rewrite_tool_body(text: str, prefix: str, segments: List[str]) -> str:
    """把外部工具响应体里的**根绝对路径**改写到面板子路径下。

    这类 WebUI 普遍写死 `/api/...`、`/assets/...`，挂在 `/tools/<id>/` 下会 404。
    只改写注册表里显式声明的前缀（`proxy_prefixes`），避免误伤正文里的其它斜杠路径；
    同时覆盖 JS 的三种引号与 CSS 的 `url(` 写法。
    """
    for seg in segments or []:
        if not seg.startswith("/"):
            continue
        target = prefix + seg
        for lead in ('"', "'", "`", "("):
            text = text.replace(lead + seg, lead + target)
    return text


def rewrite_tool_location(value: str, base: str, prefix: str) -> str:
    """302/301 的 Location：站内相对路径加前缀，指向工具自身源站的绝对地址换成面板路径。"""
    if not value:
        return value
    if value.startswith("/"):
        return prefix + value
    root = base.rstrip("/")
    if value == root:
        return prefix + "/"
    if value.startswith(root + "/"):
        return prefix + value[len(root):]
    return value


def rewrite_tool_cookie(value: str, prefix: str) -> str:
    """把下载器下发的 Cookie 限制在 /tools/<id>/ 下（Path=/ → Path=/tools/<id>/）。"""
    if not value:
        return value
    return re.sub(r"(?i)(;\s*Path=)/", lambda m: m.group(1) + prefix + "/", value)


def proxy_target(tool: Dict[str, Any], subpath: str, query: str) -> Tuple[str, str]:
    """由工具配置 + 子路径拼出上游 URL；返回 (url, 错误)。"""
    base = str(tool.get("url") or "")
    parsed_base = urlparse(base)
    if parsed_base.scheme not in ("http", "https") or not parsed_base.hostname:
        return "", "外部工具地址无效"
    target = base.rstrip("/") + "/" + subpath.lstrip("/")
    if query:
        target += "?" + query
    parsed = urlparse(target)
    # 只允许打到配置里那一台主机，防止用 ../ 或 // 把请求引到别处（SSRF）
    if (parsed.scheme, parsed.hostname, parsed.port) != (
            parsed_base.scheme, parsed_base.hostname, parsed_base.port):
        return "", "外部工具路径非法"
    return target, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """反代必须把 3xx **原样回给浏览器**：让 urllib 自己跟随会把
    下载器的 /login 跳到上游本地（甚至跟丢），浏览器看到的就不是工具的页面了。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


_PROXY_OPENER = urllib.request.build_opener(_NoRedirect)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

Route = Tuple[str, re.Pattern, Callable]


def _json_response(handler: "PanelHandler", payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: "PanelHandler", message: str, status: int = 400) -> None:
    _json_response(handler, {"ok": False, "error": message}, status)


def _work_dir(name: str) -> Optional[Path]:
    """把作品名解析成目录，并确保它确实是工作区（works/）内的作品目录。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    ws_root = library.workspace_root()
    candidate = (ws_root / name).resolve()
    try:
        candidate.relative_to(ws_root)
    except ValueError:
        return None
    if not library.is_work_dir(candidate):
        return None
    return candidate


# ---------------------------------------------------------------------------
# API 实现
# ---------------------------------------------------------------------------

def api_health(handler, match, body, query) -> None:
    _json_response(handler, {
        "ok": True,
        "service": "novel-webpanel",
        "python": sys.executable,
        "project_root": str(_PROJECT_ROOT),
        "workspace_root": str(library.workspace_root()),
        "auth_required": bool(AUTH_TOKEN),
        "tool_proxy": bool(TOOL_PROXY_ENABLED),
        "job": jobs_service.latest(),
    })


def api_works(handler, match, body, query) -> None:
    _json_response(handler, {"ok": True, "works": library.list_works()})


def api_chapters(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    _json_response(handler, {
        "ok": True,
        "work": work.name,
        "prompt": library.work_prompt(work),
        "chapters": library.chapter_inventory(work),
    })


def api_chapter(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        number = int(match.group("num"))
    except ValueError:
        return _error(handler, "章节号无效")
    data = library.read_chapter(work, number)
    data["ok"] = True
    data["work"] = work.name
    _json_response(handler, data)


def api_glossary(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    tracker = library.read_tracker(work)
    global_glossary = library.read_global_glossary()
    # 把全局术语并进总表一起展示：chapter = -1 表示"全局"，跨作品共用。
    # 作品 tracker 里同名的条目让位给全局值（全局是更高基准）。
    per_chapter: Dict[str, Any] = {}
    for number in library.chapter_inventory(work):
        gloss = library.read_glossary(work, number["num"])
        if gloss:
            per_chapter[str(number["num"])] = gloss
    _json_response(handler, {"ok": True, "work": work.name, "tracker": tracker,
                             "global_count": sum(len(global_glossary.get(c) or {})
                                                  for c in library.glossary_categories()),
                             "chapters": per_chapter,
                             "conflicts": library.read_conflicts(work)})


def api_conflicts(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    records = library.read_conflicts(work)
    _json_response(handler, {
        "ok": True,
        "work": work.name,
        "conflicts": records,
        "blocked": len([r for r in records if r.get("action") == "ignored"]),
        "overridden": len([r for r in records if r.get("action") == "overridden"]),
    })


def api_search(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    text = (query.get("q") or [""])[0]
    _json_response(handler, {"ok": True, "work": work.name,
                             "hits": library.search_chapters(work, text)})


def api_translate(handler, match, body, query) -> None:
    """从面板触发翻译（后台任务）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    chapter = str(body.get("chapter") or "").strip()
    if not chapter:
        return _error(handler, "缺少章节号")
    result = jobs_service.start_translate(
        work.name, chapter,
        model=str(body.get("model") or ""),
        rag=bool(body.get("rag")),
        force=bool(body.get("force")),
        rebuild_glossary=bool(body.get("rebuild_glossary")),
    )
    _json_response(handler, result, 200 if result.get("ok") else 409)


def api_retranslate(handler, match, body, query) -> None:
    """局部重译指定段落。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        chapter = int(body.get("chapter"))
    except (TypeError, ValueError):
        return _error(handler, "缺少或非法的章节号")
    rows = [int(r) for r in (body.get("rows") or []) if str(r).strip().isdigit()]
    if not rows:
        return _error(handler, "没有选中任何段落")
    result = jobs_service.start_retranslate(
        work.name, chapter, sorted(set(rows)),
        model=str(body.get("model") or ""),
        rag=bool(body.get("rag")),
        allow_term_changes=bool(body.get("allow_term_changes")),
    )
    _json_response(handler, result, 200 if result.get("ok") else 409)


def api_snippets(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    _json_response(handler, snippets_service.get_all(work.name))


def api_snippets_save(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    result = snippets_service.save(work.name, str(body.get("kind") or ""),
                                   str(body.get("text") or ""))
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_refine(handler, match, body, query) -> None:
    """Refine：逐段点评并修订（rows 为空 = 全章）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        chapter = int(body.get("chapter"))
    except (TypeError, ValueError):
        return _error(handler, "缺少或非法的章节号")
    rows = [int(r) for r in (body.get("rows") or []) if str(r).strip().isdigit()]
    result = jobs_service.start_refine(
        work.name, chapter, sorted(set(rows)) or None,
        model=str(body.get("model") or ""), rag=bool(body.get("rag")),
        allow_term_changes=bool(body.get("allow_term_changes")),
        ignore_draft=bool(body.get("ignore_draft")),
    )
    _json_response(handler, result, 200 if result.get("ok") else 409)


def api_global_glossary(handler, match, body, query) -> None:
    """全局术语库：所有作品共用的那一层（独立视图，不属于任何作品）。"""
    _json_response(handler, glossary_edit_service.read_global(
        str(nkconfig.GLOBAL_GLOSSARY_PATH)))


def api_global_glossary_term(handler, match, body, query) -> None:
    """往全局术语库里新增/修改一条（同样遵守先到先得，需 force 才覆盖）。"""
    try:
        chapter = body.get("chapter")
        chapter_num = int(chapter) if chapter not in (None, "") else None
    except (TypeError, ValueError):
        return _error(handler, "章节号无效")
    result = glossary_edit_service.add_term(
        library.workspace_root(), str(nkconfig.GLOBAL_GLOSSARY_PATH),
        term=str(body.get("term") or ""),
        translation=str(body.get("translation") or ""),
        category=str(body.get("category") or "fixed_terms"),
        context=str(body.get("context") or ""),
        chapter=chapter_num,
        force=bool(body.get("force")),
        scope="global",
    )
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_global_glossary_lookup(handler, match, body, query) -> None:
    """在全局术语库里查一条（提交前提示会不会撞已有译法）。"""
    term = (query.get("term") or [""])[0]
    _json_response(handler, glossary_edit_service.search(
        _PROJECT_ROOT, str(nkconfig.GLOBAL_GLOSSARY_PATH), term))


def api_glossary_term(handler, match, body, query) -> None:
    """手工新增/修正一条术语（尊重先到先得，全局 glossary.json 不可覆盖）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    chapter = body.get("chapter")
    try:
        chapter_num = int(chapter) if chapter not in (None, "") else None
    except (TypeError, ValueError):
        return _error(handler, "章节号无效")
    result = glossary_edit_service.add_term(
        work, str(nkconfig.GLOBAL_GLOSSARY_PATH),
        term=str(body.get("term") or ""),
        translation=str(body.get("translation") or ""),
        category=str(body.get("category") or "fixed_terms"),
        context=str(body.get("context") or ""),
        chapter=chapter_num,
        force=bool(body.get("force")),
        scope=str(body.get("scope") or "chapter"),
    )
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_glossary_term_lookup(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    term = (query.get("term") or [""])[0]
    _json_response(handler, glossary_edit_service.search(
        work, str(nkconfig.GLOBAL_GLOSSARY_PATH), term))


def api_glossary_term_delete(handler, match, body, query) -> None:
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    result = glossary_edit_service.delete_term(
        work, str(nkconfig.GLOBAL_GLOSSARY_PATH),
        term=str(body.get("term") or ""), context=str(body.get("context") or ""),
    )
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_chapter_delete(handler, match, body, query) -> None:
    """删除一章（正文各文件 + 本章术语，先备份到 .backups/）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        number = int(match.group("num"))
    except ValueError:
        return _error(handler, "章节号无效")
    try:
        result = editor_service.delete_chapter(
            work, number, include_glossary=bool(body.get("include_glossary", True)))
    except editor_service.EditorError as exc:
        return _error(handler, str(exc), 400)
    _json_response(handler, result)


def api_work_rename(handler, match, body, query) -> None:
    """给作品目录改名（作品名就是目录名）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        result = works_service.rename(work, str(body.get("name") or ""),
                                      root=library.workspace_root())
    except works_service.WorkError as exc:
        return _error(handler, str(exc), 400)
    _json_response(handler, result)


def api_work_delete(handler, match, body, query) -> None:
    """把整部作品移入工作区根的 .trash/（不是真删，随时可恢复）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        result = works_service.delete(work, root=library.workspace_root())
    except works_service.WorkError as exc:
        return _error(handler, str(exc), 400)
    _json_response(handler, result)


def api_tools(handler, match, body, query) -> None:
    _json_response(handler, {"ok": True, "tools": tools_service.list_tools()})


def api_tool_update(handler, match, body, query) -> None:
    result = tools_service.set_url(str(body.get("id") or ""), str(body.get("url") or ""))
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_jobs(handler, match, body, query) -> None:
    _json_response(handler, {"ok": True, "latest": jobs_service.latest()})


def api_job_detail(handler, match, body, query) -> None:
    job = jobs_service.get(match.group("job"))
    if job is None:
        return _error(handler, "任务不存在", 404)
    _json_response(handler, {"ok": True, "job": job})


def api_job_cancel(handler, match, body, query) -> None:
    result = jobs_service.cancel(match.group("job"))
    _json_response(handler, result, 200 if result.get("ok") else 400)


# SSE：任务日志实时推送，替代前端每 1.5s 轮询 /api/jobs/<id>。
SSE_POLL_SECONDS = 0.25        # 检查日志是否有新增的间隔
SSE_KEEPALIVE_SECONDS = 15.0   # 空闲时发注释行，避免中间层掐掉长连接


def _sse_frame(event: str, payload: Any) -> bytes:
    """SSE 帧。用 JSON 承载内容，换行/引号都由 JSON 转义，不用担心多行 data。"""
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def _sse_write(handler, frame: bytes) -> bool:
    """写一帧；客户端已断开时安静地返回 False，不往日志里抛栈。"""
    try:
        handler.wfile.write(frame)
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def api_job_stream(handler, match, body, query) -> None:
    """`GET /api/jobs/<id>/stream`：任务日志的 SSE 流。

    事件：
      init    {log, job}   建连：先给一段日志尾部与当前任务状态
      append  {chunk}      新增输出（可能连续多次）
      end     {job}        任务已进入终态且输出读完；前端应关闭连接
    """
    job_id = match.group("job")
    job = jobs_service.get(job_id)
    if job is None:
        return _error(handler, "任务不存在", 404)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("X-Accel-Buffering", "no")   # 让 Nginx 不要缓冲
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True   # 流结束后不复用这条连接

    # 任务元信息里没必要再带一份日志（init 已经单独给了）
    meta = {k: v for k, v in job.items() if k != "log"}

    first = jobs_service.log_read(job_id, offset=-1)
    if not _sse_write(handler, _sse_frame("init", {"log": first["text"], "job": meta})):
        return
    offset = first["offset"]
    last_ping = time.monotonic()

    while True:
        part = jobs_service.log_read(job_id, offset=offset)
        offset = part["offset"]
        if part["text"]:
            if not _sse_write(handler, _sse_frame("append", {"chunk": part["text"]})):
                return
            last_ping = time.monotonic()
            continue

        current = jobs_service.get(job_id)
        if current is None:
            _sse_write(handler, _sse_frame("end", {"job": None}))
            return
        if current["state"] != "running":
            # 进程退出到日志落盘之间有个极小窗口，收尾时再读一次，别漏掉最后几行
            final = jobs_service.log_read(job_id, offset=offset)
            if final["text"]:
                _sse_write(handler, _sse_frame("append", {"chunk": final["text"]}))
            _sse_write(handler, _sse_frame("end",
                                           {"job": {k: v for k, v in current.items() if k != "log"}}))
            return

        if time.monotonic() - last_ping >= SSE_KEEPALIVE_SECONDS:
            if not _sse_write(handler, b": ping\n\n"):
                return
            last_ping = time.monotonic()
        time.sleep(SSE_POLL_SECONDS)


def api_credentials(handler, match, body, query) -> None:
    _json_response(handler, creds_service.status())


def api_credentials_save(handler, match, body, query) -> None:
    result = creds_service.save(body)
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_credentials_verify(handler, match, body, query) -> None:
    """用当前 API Key 与地址向模型服务发一次最小请求，验证配置可用。"""
    result = creds_service.verify_model_api()
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_epub_scan(handler, match, body, query) -> None:
    payload = str(body.get("data") or "")
    if not payload:
        return _error(handler, "缺少上传内容")
    try:
        data = epub_service.decode_base64(payload)
        result = epub_service.scan_bytes(data, str(body.get("filename") or "upload.epub"))
    except epub_service.EpubError as exc:
        return _error(handler, str(exc))
    _json_response(handler, result)


def api_epub_commit(handler, match, body, query) -> None:
    try:
        result = epub_service.commit(
            str(body.get("scan_id") or ""),
            str(body.get("work") or ""),
            include_intro=bool(body.get("include_intro", True)),
            intro_index=(int(body["intro_index"]) if body.get("intro_index") not in (None, "")
                         else None),
            body_indices=[int(x) for x in (body.get("body_indices") or [])],
            start_number=int(body.get("start_number") or 1),
            overwrite=bool(body.get("overwrite")),
        )
    except epub_service.EpubError as exc:
        return _error(handler, str(exc))
    except (TypeError, ValueError) as exc:
        return _error(handler, f"参数错误: {exc}")
    _json_response(handler, result, 200 if result.get("ok") else 400)


def api_chapter_inventory(handler, match, body, query) -> None:
    """章号清单：最大章号、缺译文/缺原文的章节，供编辑器定位。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    _json_response(handler, editor_service.inventory(work))


def api_chapter_text(handler, match, body, query) -> None:
    """读取一章原文或译文的纯文本，供本地编辑器加载。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        number = int(match.group("num"))
    except ValueError:
        return _error(handler, "章节号无效")
    side = (query.get("side") or ["translated"])[0]
    if side not in ("origin", "translated"):
        return _error(handler, "side 只能是 origin 或 translated")
    try:
        text = editor_service.read_text(work, number, side)
    except editor_service.EditorError as exc:
        return _error(handler, str(exc))
    _json_response(handler, {"ok": True, "work": work.name, "num": number,
                             "side": side, "text": text, "path":
                             f"{number}_{side}.txt"})


def api_chapter_save(handler, match, body, query) -> None:
    """保存一章原文或译文（写前自动备份到 .backups/）。"""
    work = _work_dir(match.group("work"))
    if work is None:
        return _error(handler, "作品不存在", 404)
    try:
        number = int(match.group("num"))
    except ValueError:
        return _error(handler, "章节号无效")
    side = str(body.get("side") or "translated")
    if side not in ("origin", "translated"):
        return _error(handler, "side 只能是 origin 或 translated")
    try:
        result = editor_service.save_text(work, number, side, body.get("text"),
                                          create=bool(body.get("create")))
    except editor_service.EditorConflict as exc:
        return _error(handler, str(exc), 409)
    except editor_service.EditorError as exc:
        return _error(handler, str(exc), 400)
    _json_response(handler, result)


ROUTES: List[Route] = [
    ("GET", re.compile(r"^/api/health$"), api_health),
    ("GET", re.compile(r"^/api/works$"), api_works),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/rename$"), api_work_rename),
    ("DELETE", re.compile(r"^/api/works/(?P<work>[^/]+)$"), api_work_delete),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/chapters$"), api_chapters),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/chapter/(?P<num>\d+)$"), api_chapter),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/glossary$"), api_glossary),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/conflicts$"), api_conflicts),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/search$"), api_search),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/inventory$"), api_chapter_inventory),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/text/(?P<num>\d+)$"), api_chapter_text),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/text/(?P<num>\d+)$"), api_chapter_save),
    ("DELETE", re.compile(r"^/api/works/(?P<work>[^/]+)/chapter/(?P<num>\d+)$"), api_chapter_delete),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/translate$"), api_translate),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/retranslate$"), api_retranslate),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/refine$"), api_refine),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/snippets$"), api_snippets),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/snippets$"), api_snippets_save),
    ("GET", re.compile(r"^/api/works/(?P<work>[^/]+)/glossary/lookup$"), api_glossary_term_lookup),
    ("POST", re.compile(r"^/api/works/(?P<work>[^/]+)/glossary/term$"), api_glossary_term),
    ("DELETE", re.compile(r"^/api/works/(?P<work>[^/]+)/glossary/term$"), api_glossary_term_delete),
    ("GET", re.compile(r"^/api/glossary/global$"), api_global_glossary),
    ("POST", re.compile(r"^/api/glossary/global/term$"), api_global_glossary_term),
    ("GET", re.compile(r"^/api/glossary/global/lookup$"), api_global_glossary_lookup),
    ("GET", re.compile(r"^/api/tools$"), api_tools),
    ("POST", re.compile(r"^/api/tools$"), api_tool_update),
    ("GET", re.compile(r"^/api/jobs$"), api_jobs),
    ("GET", re.compile(r"^/api/jobs/(?P<job>[0-9a-f]+)$"), api_job_detail),
    ("GET", re.compile(r"^/api/jobs/(?P<job>[0-9a-f]+)/stream$"), api_job_stream),
    ("POST", re.compile(r"^/api/jobs/(?P<job>[0-9a-f]+)/cancel$"), api_job_cancel),
    ("GET", re.compile(r"^/api/settings/credentials$"), api_credentials),
    ("POST", re.compile(r"^/api/settings/credentials$"), api_credentials_save),
    ("POST", re.compile(r"^/api/settings/credentials/verify$"), api_credentials_verify),
    ("POST", re.compile(r"^/api/import/epub/scan$"), api_epub_scan),
    ("POST", re.compile(r"^/api/import/epub/commit$"), api_epub_commit),
]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class PanelHandler(BaseHTTPRequestHandler):
    server_version = "NovelPanel/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础 ---------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认 access log
        if os.environ.get("PANEL_VERBOSE"):
            super().log_message(fmt, *args)

    def _client_is_local(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return host in ("127.0.0.1", "::1", "localhost")

    # -- 认证 ---------------------------------------------------------------

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — 畸形 Cookie 视为未登录
            return ""
        morsel = jar.get(AUTH_COOKIE)
        return morsel.value if morsel else ""

    def _authenticated(self) -> bool:
        return token_ok(self._cookie_token())

    def _set_cookie(self, value: str, *, clear: bool = False) -> None:
        cookie = f"{AUTH_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax"
        # 走 HTTPS 反向代理时（X-Forwarded-Proto: https）补上 Secure
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            cookie += "; Secure"
        if clear:
            cookie = f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        self.send_header("Set-Cookie", cookie)

    def _redirect(self, location: str, *, clear_cookie: bool = False,
                  token: Optional[str] = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if token is not None:
            self._set_cookie(token)
        elif clear_cookie:
            self._set_cookie("", clear=True)
        self.end_headers()

    def _unauthorized(self, path: str, method: str, next_url: str) -> None:
        if path.startswith("/api/"):
            return _json_response(self, {
                "ok": False, "error": "未认证：请先登录", "auth_required": True,
            }, 401)
        target = "/login" + (f"?next={quote(next_url)}" if next_url else "")
        return self._redirect(target)

    # -- 登录页 -------------------------------------------------------------

    def _login_page(self, *, error: str = "", next_url: str = "") -> None:
        next_input = (f'<input type="hidden" name="next" value="{html.escape(next_url)}">'
                      if next_url else "")
        error_html = (f'<p class="err">{html.escape(error)}</p>' if error else "")
        body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · NovelKit</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0f1115; color: #e6e6e9;
         font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
  form {{ width: min(360px, 92vw); background: #171a21; border: 1px solid #262b36;
          border-radius: 12px; padding: 22px; }}
  h1 {{ font-size: 16px; margin: 0 0 4px; }}
  p.hint {{ margin: 0 0 16px; color: #8b93a7; font-size: 12px; }}
  p.err {{ margin: 0 0 12px; color: #f85149; font-size: 12px; }}
  input {{ width: 100%; padding: 9px 11px; border-radius: 8px; border: 1px solid #2c3340;
           background: #0f1115; color: inherit; font: inherit; }}
  input:focus {{ outline: none; border-color: #4c8dff; }}
  button {{ margin-top: 12px; width: 100%; padding: 9px 11px; border: 0; border-radius: 8px;
            background: #2f6fed; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }}
  button:hover {{ background: #3b7cf7; }}
</style></head>
<body>
  <form method="get" action="/login">
    <h1>NovelKit · 网文翻译工作台</h1>
    <p class="hint">这是一个受保护的实例，请输入部署时设置的访问令牌。</p>
    {error_html}
    {next_input}
    <input type="password" name="token" placeholder="访问令牌" autofocus autocomplete="current-password">
    <button type="submit">进入面板</button>
  </form>
</body></html>"""
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_auth(self, path: str, method: str, query: Dict[str, List[str]]) -> bool:
        """返回 True 表示请求已被认证流程处理（调用方应立即返回）。

        认证关闭（本机模式）时永远返回 False，由调用方走原来的本机判断。
        """
        if not AUTH_TOKEN:
            if path in PUBLIC_PATHS:      # 没开认证时 /login、/logout 直接回首页
                self._redirect("/")
                return True
            return False

        supplied = (query.get("token") or [""])[0]
        next_url = (query.get("next") or [""])[0]
        if next_url and not next_url.startswith("/"):
            next_url = ""      # 只接受站内相对路径，避免开放重定向

        # 一次性登录链接：/ 或 /login?token=xxx → 换 Cookie 后跳转
        if supplied:
            if token_ok(supplied):
                self._redirect(next_url or "/", token=supplied)
            elif path == "/login":
                self._login_page(error="访问令牌不正确", next_url=next_url)
            else:
                self._unauthorized(path, method, next_url)
            return True

        if path == "/logout":
            self._redirect("/login", clear_cookie=True)
            return True

        if self._authenticated():
            if path == "/login":
                self._redirect(next_url or "/")
                return True
            return False

        if path == "/login":
            self._login_page(next_url=next_url)
        else:
            self._unauthorized(path, method, next_url)
        return True

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_raw_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    # -- 外部工具反代 -------------------------------------------------------

    def _proxy_tool(self, path: str, method: str) -> None:
        if not TOOL_PROXY_ENABLED:
            return _error(self, "面板的外部工具反代已关闭（--no-tool-proxy）", 403)

        rest = path[len("/tools/"):]
        tool_id, _, subpath = rest.partition("/")
        tool = tools_service.get(tool_id)
        if tool is None:
            return _error(self, "未知工具", 404)

        # /tools/tomato → /tools/tomato/：有了结尾斜杠，页面的相对路径才解析正确
        if not path.endswith("/") and not subpath:
            tail = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
            return self._redirect(path + "/" + tail)

        target, error = proxy_target(tool, subpath, self.path.split("?", 1)[1] if "?" in self.path else "")
        if error:
            return _error(self, error, 400)

        prefix = _proxy_prefix(tool_id)
        headers: Dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            headers[key] = value
        # 上游可能按 gzip 返回，压缩后的字节没法做前缀改写，所以显式要 identity
        headers["Accept-Encoding"] = "identity"

        data: Optional[bytes] = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                data = self._read_raw_body() or None
            except ValueError as exc:
                return _error(self, str(exc), 413)

        request = urllib.request.Request(target, data=data, headers=headers, method=method)
        try:
            with _PROXY_OPENER.open(request, timeout=PROXY_TIMEOUT) as response:
                status, raw, reply_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:      # 4xx/5xx 也要原样回给浏览器
            status, raw, reply_headers = exc.code, exc.read(), exc.headers
        except Exception as exc:  # noqa: BLE001 — 连接被拒/超时等
            return _error(self, f"连接外部工具失败：{type(exc).__name__}: {exc}", 502)

        ctype_full = reply_headers.get("Content-Type") or ""
        ctype = ctype_full.split(";")[0].strip().lower()
        encoding = (reply_headers.get("Content-Encoding") or "").strip().lower()
        rewritten = ctype in _PROXY_TEXT_TYPES and encoding in ("", "identity")
        body = raw
        if rewritten:
            charset = "utf-8"
            match = re.search(r"charset=([\w\-]+)", ctype_full, re.I)
            if match:
                charset = match.group(1)
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            text = rewrite_tool_body(text, prefix, tool.get("proxy_prefixes") or [])
            body = text.encode("utf-8")

        self.send_response(status)
        for key, value in reply_headers.items():
            low = key.lower()
            if low in _HOP_BY_HOP or low in ("content-length", "location", "set-cookie"):
                continue
            if rewritten and low in ("content-encoding", "content-type"):
                continue          # 重写后是 UTF-8 明文，由下面重新声明
            self.send_header(key, value)
        if rewritten:
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        location = reply_headers.get("Location")
        if location:
            self.send_header("Location", rewrite_tool_location(location, str(tool["url"]), prefix))
        for cookie in (reply_headers.get_all("Set-Cookie") or []):
            self.send_header("Set-Cookie", rewrite_tool_cookie(cookie, prefix))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        # 认证优先：配置了令牌就一律校验（本机也一样，浏览器统一走 /login）。
        # 未配置令牌时退回"仅本机"，与旧行为一致。
        if self._handle_auth(path, method, query):
            return
        if not AUTH_TOKEN and not self._client_is_local():
            return _error(self, "仅允许本机访问", 403)

        if path == "/tools" or path.startswith("/tools/"):
            return self._proxy_tool(path if path != "/tools" else "/tools/", method)

        if path.startswith("/api/"):
            body: Dict[str, Any] = {}
            if method in ("POST", "DELETE"):
                # 只接受 JSON 请求体：跨站表单是简单请求，拿不到 application/json，
                # 于是这条检查顺手挡掉了 CSRF（Cookie 本身也设了 SameSite=Lax）。
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and ctype != "application/json":
                    return _error(self, "Content-Type 必须是 application/json", 415)
                try:
                    body = self._read_body()
                except ValueError as exc:
                    return _error(self, str(exc), 413)
            # 同一个路径可能同时注册了 GET 与 POST（例如 /api/settings/credentials）。
            # 因此方法不匹配时必须**继续找**，不能立刻返回 405 ——
            # 否则 POST 会先撞上先注册的 GET 路由，永远到不了 POST 处理函数。
            path_known = False
            for route_method, pattern, view in ROUTES:
                match = pattern.match(path)
                if not match:
                    continue
                path_known = True
                if route_method != method:
                    continue
                try:
                    return view(self, match, body, query)
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    return _error(self, f"{type(exc).__name__}: {exc}", 500)
            if path_known:
                return _error(self, f"方法不允许：{method} {path}", 405)
            return _error(self, f"未知接口: {path}", 404)

        if method != "GET":
            return _error(self, "方法不允许", 405)
        if path in ("/", "/index.html"):
            return self._serve_file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        # 单页应用：未知路径回落到首页
        return self._serve_file(STATIC_DIR / "index.html")

    def _serve_static(self, relative: str) -> None:
        relative = relative.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return _error(self, "非法路径", 403)
        if not target.is_file():
            return _error(self, "文件不存在", 404)
        return self._serve_file(target)

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return _error(self, "页面不存在", 404)
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


# ---------------------------------------------------------------------------

def serve(host: str = "127.0.0.1", port: int = 8897, open_browser: bool = False,
          token: str = "", tool_proxy: bool = True) -> None:
    global AUTH_TOKEN, TOOL_PROXY_ENABLED
    AUTH_TOKEN = (token or "").strip()
    TOOL_PROXY_ENABLED = bool(tool_proxy)

    # 首次运行时工作区可能还不存在：建出来，面板与导入都能直接用。
    try:
        library.workspace_root().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    httpd = ThreadingHTTPServer((host, port), PanelHandler)
    httpd.daemon_threads = True
    shown = "127.0.0.1" if is_loopback_host(host) else host
    url = f"http://{shown}:{port}/"
    print(f"[面板] 项目目录: {_PROJECT_ROOT}")
    print(f"[面板] 工作区:   {library.workspace_root()}")
    print(f"[面板] 地址:   {url}")
    if AUTH_TOKEN:
        print(f"[面板] 已启用访问令牌。首次打开（或换浏览器）：{url}?token=<令牌>")
    else:
        print("[面板] 未设置访问令牌：仅接受本机（127.0.0.1）来源。")
    if TOOL_PROXY_ENABLED:
        print("[面板] 外部工具反代: 已启用（/tools/<id>/）")
    else:
        print("[面板] 外部工具反代: 已关闭（--no-tool-proxy）")
    print("[面板] Ctrl+C 停止")
    if open_browser:
        # 带令牌时把一次性登录链接给浏览器，省去手输。
        target = f"{url}?token={quote(AUTH_TOKEN)}" if AUTH_TOKEN else url
        threading.Timer(0.6, lambda: webbrowser.open(target)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[面板] 已停止")
    finally:
        httpd.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    env_host = (os.environ.get("PANEL_HOST") or "").strip()
    env_port = (os.environ.get("PANEL_PORT") or "").strip()
    env_token = (os.environ.get("PANEL_TOKEN") or "").strip()

    parser = argparse.ArgumentParser(
        description="网文项目 Web 面板（默认仅本机；远程访问必须设置访问令牌）")
    parser.add_argument("--host", default=env_host or "127.0.0.1",
                        help="监听地址（默认 127.0.0.1；云服务器用 0.0.0.0，需配 --token）")
    parser.add_argument("--port", type=int, default=int(env_port) if env_port.isdigit() else 8897,
                        help="监听端口（默认 8897）")
    parser.add_argument("--token", default=env_token,
                        help="访问令牌（也可用环境变量 PANEL_TOKEN）；远程访问必填")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-tool-proxy", dest="tool_proxy", action="store_false",
                        help="关闭 /tools/<id>/ 反向代理（侧栏入口改为直连外部工具地址）")
    parser.add_argument("--tool-proxy", dest="tool_proxy", action="store_true",
                        help="启用外部工具反向代理（默认；也可用 PANEL_TOOL_PROXY=1）")
    parser.set_defaults(tool_proxy=(os.environ.get("PANEL_TOOL_PROXY") or "1").strip().lower()
                        not in ("0", "false", "no", "off"))
    args = parser.parse_args(argv)

    token = (args.token or "").strip()
    if not is_loopback_host(args.host) and not token:
        print("[错误] 监听非本机地址时必须设置访问令牌，否则面板会对公网裸奔。",
              file=sys.stderr)
        print("       例如： PANEL_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))') \\",
              file=sys.stderr)
        print("              .venv/bin/python webpanel/server.py --host 0.0.0.0", file=sys.stderr)
        return 2

    serve(args.host, args.port, args.open, token, args.tool_proxy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
