#!/usr/bin/env python3
"""novelkit + scripts/translate.py 的离线测试（不联网、不需要 openai 依赖）。

覆盖的都是旧版真实踩过的坑：
  1. 模型返回纯文本/思考过程而非 JSON 时的处理与兜底
  2. 返回空 translated_content 时绝不能再写出空章节文件
  3. 网络抖动后的自动重试
  4. 模型不支持 response_format / extra_body 时的参数降级
  5. new_contextual_terms 结构不规范时的容错
  6. 章节号语法（空格/逗号/区间/【】/all）
  7. 术语过滤的整词匹配
  8. 术语库文件读取由 O(n²) 降为 O(n)
  9. 原子写入不产生半截文件

运行： python3 tests/test_novelkit.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from novelkit import glossary as nkglossary  # noqa: E402
from novelkit import llm  # noqa: E402
from novelkit import text as nktext  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, func) -> None:
    try:
        func()
    except Exception:  # noqa: BLE001
        RESULTS.append((name, False, traceback.format_exc(limit=3)))
    else:
        RESULTS.append((name, True, ""))


def expect(condition, message: str = "") -> None:
    if not condition:
        raise AssertionError(message or "断言失败")


# --------------------------------------------------------------------------
# 假客户端
# --------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


class FakeResponse:
    def __init__(self, content=None, reasoning=None):
        self.choices = [SimpleNamespace(message=FakeMessage(content, reasoning))]
        self.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else FakeResponse("{}")
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


class FakeHTTPError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# 1. text 工具
# --------------------------------------------------------------------------

def test_parse_chapters() -> None:
    cases = {
        "1-4 6 8-9": [1, 2, 3, 4, 6, 8, 9],
        "10,15,17,19,39,41,51": [10, 15, 17, 19, 39, 41, 51],
        "1, 3-4": [1, 3, 4],
        "【1-4】": [1, 2, 3, 4],
        "2-3": [2, 3],
        "5": [5],
        "  7 , 9  ": [7, 9],
        "4-2": [2, 3, 4],
    }
    for spec, expected in cases.items():
        got = nktext.parse_chapters(spec)
        expect(got == expected, f"{spec!r} -> {got} != {expected}")

    expect(nktext.parse_chapters("all", allow_all=True) is None)
    expect(nktext.parse_chapters("", allow_all=True) is None)
    expect(nktext.parse_chapters(None, allow_all=True) is None)

    for bad in ("abc", "1-", "1,,x"):
        try:
            nktext.parse_chapters(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应当拒绝 {bad!r}")


def test_normalize_punctuation() -> None:
    raw = "他说：“你好，世界。”\nＡＢＣ１２３\n*强调*"
    out = nktext.normalize_punctuation(raw)
    expect("，" not in out and "。" not in out and "“" not in out, f"中文标点未清理: {out!r}")
    expect("ABC123" in out, f"全角字符未还原: {out!r}")
    expect("*" not in out, f"星号未删除: {out!r}")

    kept = nktext.normalize_punctuation("*强调*", strip_asterisk=False)
    expect("*" in kept, "strip_asterisk=False 时应保留星号")

    expect(nktext.normalize_punctuation("") == "")


def test_detect_issues() -> None:
    content = "Line one is fine.\n第二行有中文。\nThird line has emoji \U0001F600 here."
    issues = nktext.detect_issues(content)
    expect(issues["has_cjk"] and issues["cjk_count"] >= 5, str(issues))
    expect(issues["cjk_lines"] == [2], f"行号错误: {issues['cjk_lines']}")
    expect(issues["has_emoji"] and issues["emoji_lines"] == [3], str(issues))


def test_chapter_heading_must_be_uniform() -> None:
    """首行章节标题必须统一成 `Chapter NN: 标题`，格式不对或整行丢失都要报出来。"""
    src = "第02章 暗夜追踪\n正文第一段。"
    expect(nktext.check_chapter_heading(src, "Chapter 02: Night Chase\nBody.") == "",
           "标准写法不应报错")
    # 用户实际踩到的：漏了冒号
    expect("冒号" in nktext.check_chapter_heading(src, "Chapter 02 Night Chase\nBody."))
    # 更严重：标题整行被吞掉，直接以正文开头
    expect("漏译" in nktext.check_chapter_heading(src, "Chen Yang walked out.\nBody."))
    # 章节号写错
    expect("章节号" in nktext.check_chapter_heading(src, "Chapter 03: Night Chase\nBody."))
    # 补零：原文不补零时也应接受两位写法
    expect(nktext.check_chapter_heading("第1章 欢迎到来", "Chapter 01: Welcome") == "")
    # 没有章节标题的文件（简介、序章）不参与校验
    expect(nktext.check_chapter_heading("Synopsis\nIntro.", "Synopsis\nIntro.") == "")


def test_atomic_write() -> None:
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, "out.txt")
        nktext.write_text_atomic(target, "hello")
        expect(Path(target).read_text(encoding="utf-8") == "hello")
        nktext.write_text_atomic(target, "world")
        expect(Path(target).read_text(encoding="utf-8") == "world")
        leftovers = [f for f in os.listdir(tmp) if f.startswith(".tmp_")]
        expect(not leftovers, f"存在残留临时文件: {leftovers}")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 2. JSON 抽取与规范化
# --------------------------------------------------------------------------

def test_extract_json() -> None:
    expect(llm.extract_json('{"a": 1}') == {"a": 1})
    expect(llm.extract_json('```json\n{"a": 2}\n```') == {"a": 2})
    expect(llm.extract_json('前缀说明\n{"a": {"b": 3}}\n后缀') == {"a": {"b": 3}})
    expect(llm.extract_json('{"a": 1,}') == {"a": 1})
    expect(llm.extract_json('{"text": "line1\nline2"}') == {"text": "line1\nline2"})

    # 带引号与嵌套的复杂场景
    tricky = '说明文字 {"translated_content": "He said \\"hello\\" {and} left.", "n": {"x": 1}} 结尾'
    expect(llm.extract_json(tricky)["n"] == {"x": 1})

    for bad in ("", "   ", "完全不是 JSON 的中文思考过程"):
        try:
            llm.extract_json(bad)
        except llm.ResponseParseError:
            pass
        else:
            raise AssertionError(f"应当解析失败: {bad!r}")


def test_coerce_payload() -> None:
    # 常见形态
    payload = llm.coerce_translation_payload(
        {
            "translated_content": "Hello.",
            "new_fixed_terms": {"林越": "Lin Yue", "坏数据": 123},
            "new_contextual_terms": {"好": {"正式": "yes"}, "简单": "simply"},
        }
    )
    expect(payload["translated_content"] == "Hello.")
    expect(payload["new_fixed_terms"] == {"林越": "Lin Yue"})
    expect(payload["new_contextual_terms"]["简单"] == {"default": "simply"})

    # 旧版会在这里崩溃：contextual 的值是字符串
    payload2 = llm.coerce_translation_payload({"译文": "Text.", "contextual_terms": {"a": "b"}})
    expect(payload2["translated_content"] == "Text.")
    expect(payload2["new_contextual_terms"]["a"] == {"default": "b"})

    # 缺少 translated_content 时取最长字符串
    payload3 = llm.coerce_translation_payload({"foo": "short", "bar": "a much longer string"})
    expect(payload3["translated_content"] == "a much longer string")

    # 列表包裹
    payload4 = llm.coerce_translation_payload([{"translated_content": "X"}])
    expect(payload4["translated_content"] == "X")


def test_salvage() -> None:
    expect(llm.salvage_plain_translation("This is plain English prose. " * 3) is not None)
    expect(llm.salvage_plain_translation("这是中文思考过程，不是译文。" * 3) is None)
    expect(llm.salvage_plain_translation("short") is None)


# --------------------------------------------------------------------------
# 3. 重试
# --------------------------------------------------------------------------

def test_retry_on_connection_error() -> None:
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("Connection error.")
        return "ok"

    result = llm.run_with_retry(flaky, retries=3, base_delay=0.0)
    expect(result == "ok" and attempts["n"] == 3, f"重试次数异常: {attempts['n']}")


def test_retry_on_parse_error() -> None:
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise llm.ResponseParseError("bad json")
        return "ok"

    expect(llm.run_with_retry(flaky, retries=2, base_delay=0.0) == "ok")


def test_no_retry_on_auth_error() -> None:
    attempts = {"n": 0}

    def fatal():
        attempts["n"] += 1
        raise FakeHTTPError("invalid api key", status_code=401)

    try:
        llm.run_with_retry(fatal, retries=5, base_delay=0.0)
    except FakeHTTPError:
        pass
    else:
        raise AssertionError("认证错误不应重试")
    expect(attempts["n"] == 1, f"认证错误被重试了 {attempts['n']} 次")


def test_is_param_error() -> None:
    expect(llm.is_param_error(FakeHTTPError("bad", 400)))
    expect(llm.is_param_error(Exception("model does not support response_format")))
    expect(not llm.is_param_error(FakeHTTPError("server", 500)))


# --------------------------------------------------------------------------
# 4. 术语库
# --------------------------------------------------------------------------

def test_glossary_word_boundary() -> None:
    tmp = tempfile.mkdtemp()
    try:
        store = nkglossary.GlossaryStore(tmp)
        glos = {
            "fixed_terms": {"Li": "Li", "林越": "Lin Yue"},
            "contextual_terms": {},
            "aesthetic_sentences": {},
            "cultural_nuances": {},
        }
        filtered = store.filter_for_content(glos, "The Liar went to the Ancient place.", "")
        expect("Li" not in filtered["fixed_terms"], "短英文键 'Li' 误命中 'Liar'")

        filtered2 = store.filter_for_content(glos, "Li nodded. 林越 smiled.", "")
        expect("Li" in filtered2["fixed_terms"], "整词 'Li' 应当命中")
        expect("林越" in filtered2["fixed_terms"], "中文键应当命中")
    finally:
        shutil.rmtree(tmp)


def test_glossary_incremental_reads() -> None:
    """核心性能断言：读取次数应为 O(n) 而不是 O(n²)。"""
    tmp = tempfile.mkdtemp()
    try:
        for i in range(1, 21):
            nktext.write_json_atomic(
                os.path.join(tmp, f"glossary_{i}.json"),
                {"fixed_terms": {f"术语{i}": f"Term{i}"}, "contextual_terms": {},
                 "aesthetic_sentences": {}, "cultural_nuances": {}},
            )

        reads = {"n": 0}
        original = nkglossary.load_glossary_file

        def counting(path):
            reads["n"] += 1
            return original(path)

        nkglossary.load_glossary_file = counting
        try:
            store = nkglossary.GlossaryStore(tmp)
            for target in range(1, 21):
                store.merged_up_to(target)
        finally:
            nkglossary.load_glossary_file = original

        # 20 个文件、20 次查询：O(n²) 需要 210 次，O(n) 只需 20 次
        expect(reads["n"] <= 25, f"文件读取次数 {reads['n']} 过高（未命中增量缓存）")
    finally:
        shutil.rmtree(tmp)


def test_tracker_first_wins() -> None:
    tmp = tempfile.mkdtemp()
    try:
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_1.json"),
            {"fixed_terms": {"A": "One"}, "contextual_terms": {"C": {"x": "1"}},
             "aesthetic_sentences": {}, "cultural_nuances": {}},
        )
        nktext.write_json_atomic(
            os.path.join(tmp, "glossary_2.json"),
            {"fixed_terms": {"A": "Two", "B": "Bee"}, "contextual_terms": {"C": {"y": "2"}},
             "aesthetic_sentences": {}, "cultural_nuances": {}},
        )
        store = nkglossary.GlossaryStore(tmp)
        path = store.write_tracker()
        tracker = json.loads(Path(path).read_text(encoding="utf-8"))
        expect(tracker["fixed_terms"]["A"]["value"] == "One", "fixed_terms 应首次记录优先")
        expect(tracker["fixed_terms"]["A"]["chapter"] == 1)
        expect(tracker["fixed_terms"]["B"]["value"] == "Bee")
        expect(tracker["contextual_terms"]["C"]["value"] == {"x": "1", "y": "2"})
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 5. 端到端：scripts/translate.py 翻译流程（使用假客户端）
# --------------------------------------------------------------------------

def make_workspace() -> str:
    tmp = tempfile.mkdtemp(prefix="novelkit_e2e_")
    Path(tmp, "0_origin.txt").write_text("简介：这是一部测试小说。", encoding="utf-8")
    Path(tmp, "1_origin.txt").write_text("第一章：主角林越登场，他获得了一百点力量。", encoding="utf-8")
    Path(tmp, "2_origin.txt").write_text("第二章：林越来到苍岚之森，遇到石昊。", encoding="utf-8")
    Path(tmp, "0_translated.txt").write_text("Introduction: a test novel.", encoding="utf-8")
    return tmp


def build_args(work_dir: str, extra: list[str]) -> argparse.Namespace:
    import translate as main_module

    return main_module.build_parser().parse_args(["--dir", work_dir, *extra])


def test_e2e_normal() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        payload = json.dumps(
            {
                "translated_content": "Chapter 2: Lin Yue arrived at the Prismatic Phantom Forest and met Shi Hao.",
                "new_fixed_terms": {"石昊": "Shi Hao"},
                "new_contextual_terms": {"幻影": {"森林语境": "phantom"}},
            },
            ensure_ascii=False,
        )
        args = build_args(tmp, ["--chapter", "2"])
        translator = main_module.Translator(args)
        translator.client = FakeClient([FakeResponse(payload)])

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)

        text = Path(tmp, "2_translated.txt").read_text(encoding="utf-8")
        expect("Prismatic Phantom Forest" in text, text)

        glos = json.loads(Path(tmp, "glossary_2.json").read_text(encoding="utf-8"))
        expect(glos["fixed_terms"]["石昊"] == "Shi Hao", str(glos))
        expect(Path(tmp, "global_glossary_tracker.json").exists())
    finally:
        shutil.rmtree(tmp)


def test_e2e_retry_then_success() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        payload = json.dumps({"translated_content": "Recovered translation text."})
        args = build_args(tmp, ["--chapter", "2", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        translator.client = FakeClient(
            [ConnectionError("Connection error."), FakeResponse(payload)]
        )

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)
        expect("Recovered" in Path(tmp, "2_translated.txt").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp)


def test_e2e_reasoning_only_does_not_write_empty_file() -> None:
    """回归测试：旧版会把空/无 JSON 的返回写成空章节文件。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        reasoning = "嗯，用户提交了一段中文小说要求翻译成英文，我需要先分析术语……" * 3
        args = build_args(tmp, ["--chapter", "2", "--retries", "1", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        translator.client = FakeClient(
            [FakeResponse("", reasoning), FakeResponse("", reasoning)]
        )

        try:
            translator.translate_chapter(2)
        except llm.ResponseParseError:
            pass
        else:
            raise AssertionError("应当抛出解析异常")

        expect(
            not Path(tmp, "2_translated.txt").exists(),
            "解析失败时绝不应写出空译文文件",
        )
        expect(not Path(tmp, "glossary_2.json").exists(), "解析失败时不应写入术语库")
    finally:
        shutil.rmtree(tmp)


def test_e2e_empty_content_retried() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        good = json.dumps({"translated_content": "Second attempt succeeded."})
        args = build_args(tmp, ["--chapter", "2", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        translator.client = FakeClient(
            [FakeResponse(json.dumps({"translated_content": ""})), FakeResponse(good)]
        )
        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)
        expect("Second attempt" in Path(tmp, "2_translated.txt").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp)


def test_e2e_plain_text_salvage() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        prose = "This chapter was returned as plain prose instead of JSON. " * 4
        args = build_args(tmp, ["--chapter", "2"])
        translator = main_module.Translator(args)
        translator.client = FakeClient([FakeResponse(prose)])

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)
        expect(outcome.salvaged, "应标记为兜底采纳")
        expect("plain prose" in Path(tmp, "2_translated.txt").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp)


def test_e2e_param_degradation() -> None:
    """模型不支持 response_format 时自动降级参数组。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        payload = json.dumps({"translated_content": "Degraded parameters worked."})
        args = build_args(tmp, ["--chapter", "2"])
        translator = main_module.Translator(args)
        client = FakeClient(
            [FakeHTTPError("response_format is not supported", 400), FakeResponse(payload)]
        )
        translator.client = client

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)
        second_call = client.chat.completions.calls[1]
        expect("response_format" not in second_call, "降级后的参数不应包含 response_format")
    finally:
        shutil.rmtree(tmp)


def test_e2e_dry_run_writes_nothing() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        args = build_args(tmp, ["--chapter", "2", "--dry-run"])
        translator = main_module.Translator(args)
        outcome = translator.translate_chapter(2)
        expect(outcome.status == "dry-run", outcome.status)
        expect(not Path(tmp, "2_translated.txt").exists(), "dry-run 不应写文件")
        expect(not Path(tmp, "glossary_2.json").exists(), "dry-run 不应写术语库")

        # 回归测试：run() 的 finally 曾经无条件写 tracker，导致 dry-run 也落盘
        code = translator.run()
        expect(code == main_module.EXIT_OK, f"dry-run 退出码应为 0，实际 {code}")
        expect(
            not Path(tmp, "global_glossary_tracker.json").exists(),
            "dry-run 不应写出 global_glossary_tracker.json",
        )
    finally:
        shutil.rmtree(tmp)


def test_dry_run_does_not_touch_existing_tracker() -> None:
    """已有 tracker 时，dry-run 必须原样保留、字节不变。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        tracker = Path(tmp, "global_glossary_tracker.json")
        tracker.write_text('{"fixed_terms": {}, "contextual_terms": {}}', encoding="utf-8")
        before = tracker.read_bytes()

        translator = main_module.Translator(build_args(tmp, ["--chapter", "2", "--dry-run"]))
        translator.run()

        expect(tracker.read_bytes() == before, "dry-run 修改了已有的 tracker 文件")
    finally:
        shutil.rmtree(tmp)


def test_atomic_write_preserves_permissions() -> None:
    """原子写入不应把文件权限改成 mkstemp 的 0600。"""
    import stat as stat_mod

    tmp = tempfile.mkdtemp()
    try:
        existing = os.path.join(tmp, "existing.txt")
        Path(existing).write_text("old", encoding="utf-8")
        os.chmod(existing, 0o640)
        nktext.write_text_atomic(existing, "new")
        expect(Path(existing).read_text(encoding="utf-8") == "new")
        mode = stat_mod.S_IMODE(os.stat(existing).st_mode)
        expect(mode == 0o640, f"覆盖已有文件应保留原权限 0640，实际 {oct(mode)}")

        fresh = os.path.join(tmp, "fresh.txt")
        nktext.write_text_atomic(fresh, "data")
        fresh_mode = stat_mod.S_IMODE(os.stat(fresh).st_mode)
        expect(fresh_mode & 0o044 == 0o044, f"新建文件应可被组/其他读取，实际 {oct(fresh_mode)}")
        expect(fresh_mode == 0o644, f"新建文件权限应为 0644，实际 {oct(fresh_mode)}")
    finally:
        shutil.rmtree(tmp)


def test_e2e_zh_retry() -> None:
    """--zh-retry：译文残留中文时自动重译。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        with_cjk = json.dumps({"translated_content": "Chapter 2: 林越 arrived at the forest."})
        clean = json.dumps({"translated_content": "Chapter 2: Lin Yue arrived at the forest."})
        args = build_args(tmp, ["--chapter", "2", "--zh", "--zh-retry", "1", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        translator.client = FakeClient([FakeResponse(with_cjk), FakeResponse(clean)])

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", outcome.message)
        final = Path(tmp, "2_translated.txt").read_text(encoding="utf-8")
        expect("Lin Yue" in final, final)
        expect(not nktext.has_cjk(final), f"重译后仍有中文: {final}")
        expect(outcome.cjk_count == 0, f"cjk_count={outcome.cjk_count}")
    finally:
        shutil.rmtree(tmp)


def test_e2e_refine_failure_keeps_translation() -> None:
    """--refine 时评审失败，必须保留初译而不是丢掉整章。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        payload = json.dumps({"translated_content": "Initial translation kept."})
        args = build_args(tmp, ["--chapter", "2", "--refine", "--retries", "0", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        # 第1次(初译)成功 → 第2次(评审)失败
        translator.client = FakeClient(
            [FakeResponse(payload), ConnectionError("review backend down")]
        )

        outcome = translator.translate_chapter(2)
        expect(outcome.status == "ok", f"评审失败不应导致章节失败: {outcome.message}")
        saved = Path(tmp, "2_translated.txt").read_text(encoding="utf-8")
        expect("Initial translation kept" in saved, saved)
    finally:
        shutil.rmtree(tmp)


def test_select_targets() -> None:
    import translate as main_module

    tmp = make_workspace()
    try:
        Path(tmp, "1_translated.txt").write_text("done", encoding="utf-8")
        translator = main_module.Translator(build_args(tmp, ["--tasks", "5"]))
        expect(translator.select_targets() == [2], str(translator.select_targets()))

        forced = main_module.Translator(build_args(tmp, ["--tasks", "5", "--force"]))
        # 注意：0_origin.txt 是"作品简介"，沿用旧版行为会被当作第 0 章一起翻译，
        # 因为 0_translated.txt 正是后续章节的 style_baseline 锚点。
        expect(forced.select_targets() == [0, 1, 2], str(forced.select_targets()))

        picked = main_module.Translator(build_args(tmp, ["--chapter", "1,2"]))
        expect(picked.select_targets() == [1, 2])
    finally:
        shutil.rmtree(tmp)


def test_run_continues_after_failure() -> None:
    """旧版一章失败会 break，导致后面的章节全部不翻译。"""
    import translate as main_module

    tmp = make_workspace()
    try:
        good = json.dumps({"translated_content": "Chapter one translated fine."})
        args = build_args(tmp, ["--tasks", "2", "--retries", "0", "--retry-delay", "0"])
        translator = main_module.Translator(args)
        # 第1章始终失败（retries=0 → 只消耗一次），第2章成功
        translator.client = FakeClient(
            [ConnectionError("boom"), FakeResponse(good)]
        )
        code = translator.run()
        expect(code == main_module.EXIT_PARTIAL, f"退出码应为 1，实际 {code}")
        expect(Path(tmp, "2_translated.txt").exists(), "第 2 章应当仍被翻译")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("text: 章节号解析", test_parse_chapters),
        ("text: 标点规范化", test_normalize_punctuation),
        ("text: 中文/Emoji 检测", test_detect_issues),
        ("text: 章节标题统一", test_chapter_heading_must_be_uniform),
        ("text: 原子写入", test_atomic_write),
        ("llm : JSON 抽取", test_extract_json),
        ("llm : payload 规范化", test_coerce_payload),
        ("llm : 纯文本兜底判定", test_salvage),
        ("llm : 连接错误重试", test_retry_on_connection_error),
        ("llm : 解析错误重试", test_retry_on_parse_error),
        ("llm : 认证错误不重试", test_no_retry_on_auth_error),
        ("llm : 参数错误识别", test_is_param_error),
        ("术语: 整词匹配", test_glossary_word_boundary),
        ("术语: 增量读取 O(n)", test_glossary_incremental_reads),
        ("术语: tracker 首次优先", test_tracker_first_wins),
        ("e2e : 正常翻译落盘", test_e2e_normal),
        ("e2e : 抖动后重试成功", test_e2e_retry_then_success),
        ("e2e : 空内容不写空文件", test_e2e_reasoning_only_does_not_write_empty_file),
        ("e2e : 空译文重试", test_e2e_empty_content_retried),
        ("e2e : 纯文本兜底", test_e2e_plain_text_salvage),
        ("e2e : 参数自动降级", test_e2e_param_degradation),
        ("e2e : 中文残留自动重译", test_e2e_zh_retry),
        ("e2e : 评审失败保留初译", test_e2e_refine_failure_keeps_translation),
        ("e2e : dry-run 不写文件", test_e2e_dry_run_writes_nothing),
        ("e2e : dry-run 不动已有 tracker", test_dry_run_does_not_touch_existing_tracker),
        ("text: 原子写入保留权限", test_atomic_write_preserves_permissions),
        ("e2e : 目标章节选择", test_select_targets),
        ("e2e : 失败后继续后续章节", test_run_continues_after_failure),
    ]

    for name, func in tests:
        check(name, func)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 62)
    print(f"novelkit 测试结果: {passed}/{len(RESULTS)} 通过")
    print("=" * 62)
    for name, ok, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print("        " + detail.replace("\n", "\n        "))
    print("=" * 62)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
