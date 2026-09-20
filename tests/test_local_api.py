#!/usr/bin/env python3
"""本地 Mock API 集成测试：用真实的 openai SDK 走完整链路，但不访问外部网络。

启动一个 127.0.0.1 上的假 OpenAI 兼容服务，验证：
  1. llm.create_client 构造出的客户端能被真实 SDK 正常使用；
  2. extra_body(repetition_penalty / enable_thinking / thinking_budget) 确实被展平进请求体；
  3. response_format 被正确序列化；
  4. 服务端返回 400 "response_format is not supported" 时，参数自动降级后成功；
  5. 服务端返回 500 时，真实 InternalServerError 触发退避重试并最终成功；
  6. usage token 统计被正确读取。

若未安装 openai，则整体跳过（退出码 0）并提示。

运行： python3 tests/test_local_api.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import openai  # noqa: F401
except ImportError:
    print("[跳过] 未安装 openai，无法运行本地 Mock API 集成测试。")
    print("       安装后重试： pip install -r requirements.txt")
    raise SystemExit(0)

from novelkit import llm  # noqa: E402
from novelkit import text as nktext  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, func) -> None:
    try:
        func()
    except Exception:  # noqa: BLE001
        RESULTS.append((name, False, traceback.format_exc(limit=4)))
    else:
        RESULTS.append((name, True, ""))


def expect(condition, message: str = "") -> None:
    if not condition:
        raise AssertionError(message or "断言失败")


# --------------------------------------------------------------------------
# 假 OpenAI 服务
# --------------------------------------------------------------------------

def completion_body(payload: str) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": payload},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


def error_body(message: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": None}}


class MockState:
    def __init__(self):
        self.bodies: list[dict] = []
        self.mode = "json_unsupported"
        self.fail_first = 0
        self.reply = json.dumps({"translated_content": "Mock translated text."})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        state: MockState = self.server.state  # type: ignore[attr-defined]
        state.bodies.append(body)

        # 模式 1：不支持 response_format
        if state.mode == "json_unsupported" and "response_format" in body:
            self._send(400, error_body("response_format is not supported by this model"))
            return

        # 模式 2：前 N 次返回 500
        if state.fail_first > 0:
            state.fail_first -= 1
            self._send(500, error_body("internal server error, please try again"))
            return

        self._send(200, completion_body(state.reply))

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server(mode: str, fail_first: int = 0, reply: str | None = None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = MockState()
    state.mode = mode
    state.fail_first = fail_first
    if reply is not None:
        state.reply = reply
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}/v1"


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------

def make_workspace() -> str:
    tmp = tempfile.mkdtemp(prefix="novelkit_api_")
    Path(tmp, "0_origin.txt").write_text("简介。", encoding="utf-8")
    Path(tmp, "1_origin.txt").write_text("第一章：林越获得了一百点力量。", encoding="utf-8")
    Path(tmp, "0_translated.txt").write_text("Introduction.", encoding="utf-8")
    return tmp


def build_translator(work_dir: str, base_url: str, extra: list[str]):
    import translate as main_module

    args = main_module.build_parser().parse_args(
        ["--dir", work_dir, "--base_url", base_url, "--api_key", "sk-test", *extra]
    )
    translator = main_module.Translator(args)
    translator.client = llm.create_client(
        api_key="sk-test", base_url=base_url, timeout=args.timeout
    )
    return translator


def test_real_sdk_call_and_usage() -> None:
    server, state, base_url = start_server("always_ok")
    try:
        tmp = make_workspace()
        translator = build_translator(tmp, base_url, ["--chapter", "1"])
        outcome = translator.translate_chapter(1)

        expect(outcome.status == "ok", outcome.message)
        expect("Mock translated text." in Path(tmp, "1_translated.txt").read_text(encoding="utf-8"))
        expect(outcome.prompt_tokens == 11, f"prompt_tokens={outcome.prompt_tokens}")
        expect(outcome.completion_tokens == 22, f"completion_tokens={outcome.completion_tokens}")
        expect(len(state.bodies) == 1, f"应只请求一次，实际 {len(state.bodies)}")
    finally:
        server.shutdown()


def test_extra_body_flattened() -> None:
    """extra_body 里的自定义字段必须出现在请求体顶层。"""
    server, state, base_url = start_server("always_ok")
    try:
        tmp = make_workspace()
        translator = build_translator(
            tmp, base_url,
            ["--chapter", "1", "--thinking", "--thinking_budget", "4096", "--repetition_penalty", "1.15"],
        )
        translator.translate_chapter(1)

        body = state.bodies[0]
        expect(body.get("enable_thinking") is True, f"enable_thinking 未展平: {sorted(body)}")
        expect(body.get("thinking_budget") == 4096, f"thinking_budget={body.get('thinking_budget')}")
        expect(abs(body.get("repetition_penalty", 0) - 1.15) < 1e-6, str(body.get("repetition_penalty")))
        expect(body.get("messages") and len(body["messages"]) == 2, "messages 结构异常")
        expect(body["messages"][0]["role"] == "system", "缺少 system 消息")
    finally:
        server.shutdown()


def test_param_degradation_with_real_sdk() -> None:
    server, state, base_url = start_server("json_unsupported")
    try:
        tmp = make_workspace()
        translator = build_translator(tmp, base_url, ["--chapter", "1", "--retry-delay", "0"])
        outcome = translator.translate_chapter(1)

        expect(outcome.status == "ok", outcome.message)
        expect(len(state.bodies) >= 2, f"应当发生参数降级，实际请求 {len(state.bodies)} 次")
        expect("response_format" in state.bodies[0], "第一次请求应包含 response_format")
        expect(
            all("response_format" not in b for b in state.bodies[1:]),
            "降级后不应再发送 response_format",
        )
    finally:
        server.shutdown()


def test_retry_on_real_internal_server_error() -> None:
    server, state, base_url = start_server("always_ok", fail_first=2)
    try:
        tmp = make_workspace()
        translator = build_translator(
            tmp, base_url, ["--chapter", "1", "--retries", "3", "--retry-delay", "0"]
        )
        outcome = translator.translate_chapter(1)

        expect(outcome.status == "ok", outcome.message)
        expect(len(state.bodies) == 3, f"应为 2 次失败 + 1 次成功，实际 {len(state.bodies)}")
    finally:
        server.shutdown()


def test_no_infinite_retry_on_auth_error() -> None:
    server, state, base_url = start_server("always_ok")
    try:
        # 直接把 reply 换成鉴权错误：改用 fail_first 无法表达 401，这里换一个专用服务
        pass
    finally:
        server.shutdown()

    class AuthHandler(Handler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            state = self.server.state  # type: ignore[attr-defined]
            state.bodies.append({})
            self._send(401, error_body("invalid api key"))

    server2 = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
    state2 = MockState()
    server2.state = state2  # type: ignore[attr-defined]
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        tmp = make_workspace()
        base_url2 = f"http://127.0.0.1:{server2.server_address[1]}/v1"
        translator = build_translator(
            tmp, base_url2, ["--chapter", "1", "--retries", "5", "--retry-delay", "0"]
        )
        try:
            translator.translate_chapter(1)
        except BaseException:
            pass
        else:
            raise AssertionError("鉴权失败应当抛出异常")

        expect(len(state2.bodies) == 1, f"鉴权错误不应重试，实际请求 {len(state2.bodies)} 次")
        expect(not Path(tmp, "1_translated.txt").exists(), "失败时不应写出译文")
    finally:
        server2.shutdown()


def test_full_cli_run() -> None:
    """通过 main.main() 完整跑一遍 CLI（真实 SDK + 本地假服务）。"""
    import translate as main_module

    server, state, base_url = start_server("always_ok")
    try:
        tmp = make_workspace()
        code = main_module.main([
            "--dir", tmp, "--base_url", base_url, "--api_key", "sk-test",
            "--chapter", "1", "--zh", "--note",
        ])
        expect(code == 0, f"退出码应为 0，实际 {code}")
        expect(Path(tmp, "1_translated.txt").exists())
        expect(Path(tmp, "glossary_1.json").exists())
    finally:
        server.shutdown()


# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("真实 SDK: 调用与 usage 统计", test_real_sdk_call_and_usage),
        ("真实 SDK: extra_body 展平", test_extra_body_flattened),
        ("真实 SDK: response_format 降级", test_param_degradation_with_real_sdk),
        ("真实 SDK: 500 退避重试", test_retry_on_real_internal_server_error),
        ("真实 SDK: 401 不重试", test_no_infinite_retry_on_auth_error),
        ("真实 SDK: main() 完整 CLI", test_full_cli_run),
    ]
    for name, func in tests:
        check(name, func)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 62)
    print(f"本地 Mock API 集成测试: {passed}/{len(RESULTS)} 通过")
    print("=" * 62)
    for name, ok, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print("        " + detail.replace("\n", "\n        "))
    print("=" * 62)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
