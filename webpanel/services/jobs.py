"""后台任务：从面板触发翻译 / 回译等长耗时命令，并实时查看日志。

设计要点
--------
* **同一时刻只允许一个任务**。翻译必须按章节顺序推进（术语库是"先到先得"的，
  并发翻译同一部作品会互相污染术语），所以宁可排队也不要并行。
* 用子进程调用现有 CLI（scripts/translate.py / scripts/check.py …），不复制它们的逻辑。
* stdout+stderr 合并写入 log/panel_jobs/<job_id>.log，面板只读文件尾部，
  因此任务再长也不会把内存吃满。
* 取消 = 终止进程组，避免留下孤儿子进程。
"""

from __future__ import annotations

import datetime
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import snippets

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
_JOB_DIR = _PROJECT_ROOT / "log" / "panel_jobs"

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_ORDER: List[str] = []
_MAX_JOBS = 40
_LOG_TAIL_BYTES = 24000


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def active_job() -> Optional[Dict[str, Any]]:
    for job_id in reversed(_ORDER):
        job = _JOBS.get(job_id)
        if job and job["state"] == "running":
            return job
    return None


def start(kind: str, argv: List[str], *, label: str = "") -> Dict[str, Any]:
    """启动一个后台任务。若已有任务在跑则拒绝（返回 ok=False）。"""
    with _LOCK:
        running = active_job()
        if running is not None:
            return {
                "ok": False,
                "error": f"已有任务在运行（{running['label']}，开始于 {running['started']}），"
                         "请等它结束或先取消。翻译需要按章节顺序进行，不能并行。",
                "active": public(running),
            }

        job_id = uuid.uuid4().hex[:12]
        _JOB_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _JOB_DIR / f"{job_id}.log"

        handle = open(log_path, "w", encoding="utf-8", buffering=1)
        handle.write(f"[面板] 任务 {kind} 启动于 {_now()}\n")
        handle.write(f"[面板] 命令: {' '.join(argv)}\n")
        handle.write(f"[面板] 工作目录: {_PROJECT_ROOT}\n")
        handle.write("-" * 60 + "\n")

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(_PROJECT_ROOT),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # 便于整组终止
            )
        except Exception as exc:  # noqa: BLE001
            handle.write(f"[面板] 启动失败: {type(exc).__name__}: {exc}\n")
            handle.close()
            return {"ok": False, "error": f"启动失败: {exc}"}

        job = {
            "id": job_id,
            "kind": kind,
            "label": label or kind,
            "state": "running",
            "pid": process.pid,
            "started": _now(),
            "ended": None,
            "returncode": None,
            "log_path": str(log_path),
            "argv": argv,
            "_process": process,
            "_handle": handle,
        }
        _JOBS[job_id] = job
        _ORDER.append(job_id)
        while len(_ORDER) > _MAX_JOBS:
            oldest = _ORDER.pop(0)
            stale = _JOBS.pop(oldest, None)
            if stale and stale["state"] == "running":
                _ORDER.insert(0, oldest)  # 运行中的不淘汰
                _JOBS[oldest] = stale
                break

        threading.Thread(target=_wait, args=(job_id,), daemon=True).start()
        return {"ok": True, "job": public(job)}


def _wait(job_id: str) -> None:
    job = _JOBS.get(job_id)
    if job is None:
        return
    process: subprocess.Popen = job["_process"]
    code = process.wait()
    with _LOCK:
        # 先写 ended/returncode，最后发布终态：public() 读这些字段时不加锁，
        # 这样读侧不会看到"已结束但没有退出码"的中间态。
        job["returncode"] = code
        if job["state"] != "cancelled":
            # 取消路径已经写过终态；进程随后真的退出（码 -15）时**不要**把它盖成
            # failed —— 否则面板会先显示"已取消"、过一会儿又跳成"失败"。
            job["ended"] = _now()
            job["state"] = "done" if code == 0 else "failed"
        handle = job.get("_handle")
        if handle:
            try:
                handle.write("-" * 60 + f"\n[面板] 任务结束，退出码 {code}，于 {job['ended']}\n")
                handle.close()
            except OSError:
                pass
            job["_handle"] = None


def cancel(job_id: str) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "任务不存在"}
    if job["state"] != "running":
        return {"ok": False, "error": "任务已经结束"}

    process: subprocess.Popen = job["_process"]
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except OSError:
            pass
    with _LOCK:
        job["ended"] = _now()
        job["state"] = "cancelled"
    return {"ok": True, "job": public(job)}


def log_tail(job_id: str, *, max_bytes: int = _LOG_TAIL_BYTES) -> str:
    job = _JOBS.get(job_id)
    if job is None:
        return ""
    path = Path(job["log_path"])
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # 丢掉被截断的半行
            return handle.read()
    except OSError:
        return ""


def log_read(job_id: str, *, offset: int = 0,
             max_bytes: int = _LOG_TAIL_BYTES) -> Dict[str, Any]:
    """读取日志 [offset, EOF) 的新增片段，供 SSE 增量推送。

    offset < 0、或超出当前文件大小（日志被截断/重建）时，退化为"读末尾 max_bytes"，
    并丢掉被截断的半行。返回的 offset 是本次读到的位置（= 文件大小），调用方下次从它继续。
    """
    job = _JOBS.get(job_id)
    if job is None:
        return {"text": "", "offset": 0, "size": 0}
    path = Path(job["log_path"])
    try:
        size = path.stat().st_size
    except OSError:
        return {"text": "", "offset": 0, "size": 0}

    tail = offset < 0 or offset > size
    start = max(0, size - max_bytes) if tail else offset
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            if tail and start > 0:
                handle.readline()  # 丢掉被截断的半行
            text = handle.read()
    except OSError:
        return {"text": "", "offset": size, "size": size}
    return {"text": text, "offset": size, "size": size}


def public(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "label": job["label"],
        "state": job["state"],
        "started": job["started"],
        "ended": job["ended"],
        "returncode": job["returncode"],
        "command": " ".join(job["argv"]),
        "log_path": os.path.relpath(job["log_path"], _PROJECT_ROOT),
        "log": log_tail(job["id"]),
    }


def get(job_id: str) -> Optional[Dict[str, Any]]:
    job = _JOBS.get(job_id)
    return public(job) if job else None


def latest() -> Optional[Dict[str, Any]]:
    for job_id in reversed(_ORDER):
        job = _JOBS.get(job_id)
        if job:
            return public(job)
    return None


# ---------------------------------------------------------------------------
# 具体任务
# ---------------------------------------------------------------------------

def _attach_instruction(argv: List[str], work: str, kind: str) -> List[str]:
    """把该作品的补充要求落成临时文件并挂到命令上。"""
    path = snippets.materialise(work, kind)
    return argv + ["--instruction-file", path] if path else argv


def start_translate(work: str, chapter: str, *, model: str = "", rag: bool = False,
                    force: bool = False, rebuild_glossary: bool = False) -> Dict[str, Any]:
    """翻译指定章节。命令与手工运行 scripts/translate.py 完全一致，便于对照排查。"""
    argv = [sys.executable, str(_SCRIPTS_DIR / "translate.py"), "--dir", work, "--chapter", chapter]
    if model:
        argv += ["--model", model]
    if rag:
        argv.append("--rag")
    if force:
        argv.append("--force")
    argv.append("--zh")  # 顺手做中文残留检测，面板里能看到结果
    if rebuild_glossary:
        argv.append("--rebuild-glossary")
    argv = _attach_instruction(argv, work, "translate")
    label = f"翻译 {work} 第 {chapter} 章" + ("（RAG）" if rag else "")
    return start("translate", argv, label=label)


def start_retranslate(work: str, chapter: int, rows: List[int], *,
                      model: str = "", rag: bool = False,
                      allow_term_changes: bool = False) -> Dict[str, Any]:
    """局部重译：只重译指定段落（行号来自面板的对齐结果）。"""
    spec = ",".join(str(r) for r in rows)
    argv = [sys.executable, str(_SCRIPTS_DIR / "retranslate.py"),
            "--dir", work, "--chapter", str(chapter), "--rows", spec]
    if model:
        argv += ["--model", model]
    if rag:
        argv.append("--rag")
    if allow_term_changes:
        argv.append("--allow-term-changes")
    argv = _attach_instruction(argv, work, "retranslate")
    label = f"局部重译 {work} 第 {chapter} 章 · {len(rows)} 段"
    return start("retranslate", argv, label=label)


def start_refine(work: str, chapter: int, rows: Optional[List[int]] = None, *,
                 model: str = "", rag: bool = False,
                 allow_term_changes: bool = False,
                 ignore_draft: bool = False) -> Dict[str, Any]:
    """Refine：逐段点评并修订，附改动理由。rows 为空表示全章。"""
    argv = [sys.executable, str(_SCRIPTS_DIR / "refine.py"),
            "--dir", work, "--chapter", str(chapter)]
    if rows:
        argv += ["--rows", ",".join(str(r) for r in rows)]
    if model:
        argv += ["--model", model]
    if rag:
        argv.append("--rag")
    if allow_term_changes:
        argv.append("--allow-term-changes")
    if ignore_draft:
        argv.append("--ignore-draft")
    argv = _attach_instruction(argv, work, "refine")
    scope = f"{len(rows)} 段" if rows else "全章"
    mode = "重译" if ignore_draft else "Refine"
    return start("refine", argv, label=f"{mode} {work} 第 {chapter} 章 · {scope}")


def start_check(work: str, chapter: str = "all", *, back: bool = False,
                review: bool = False) -> Dict[str, Any]:
    argv = [sys.executable, str(_SCRIPTS_DIR / "check.py"), "--dir", work,
            "--chapter", chapter, "--zh"]
    if back:
        argv.append("--back")
    if review:
        argv.append("--review")
    return start("check", argv, label=f"校对 {work} {chapter}")
