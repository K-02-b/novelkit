#!/usr/bin/env python3
"""webpanel 的离线测试。

重点覆盖"会写文件"的路径，因为这些地方出错代价最大：
  * credentials 写 .env —— 绝不能破坏用户的其它键、绝不能回显密钥
  * 本地编辑 —— 写前备份、原子替换、路径不越界
  * EPUB 导入 —— 选择/简介/起始章号/覆盖保护
  * 段落对齐 —— Gale-Church 对齐的正确性与"不丢不重"不变量
  * 任务管理 —— 单任务互斥、日志捕获、取消

所有测试都在临时目录里跑，不碰真实 .env、作品数据与 panel_jobs。
运行： python3 tests/test_webpanel.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "webpanel"))

from services import credentials as creds  # noqa: E402
from services import import_epub as epub_import  # noqa: E402
from services import jobs as jobs_service  # noqa: E402
from services import editor as editor_service  # noqa: E402
from services import works as works_service  # noqa: E402
from services import library as lib  # noqa: E402
from services import tools as tools_service  # noqa: E402
from services import glossary_edit as glossary_edit_service  # noqa: E402
from services import snippets as snippets_service  # noqa: E402
from novelkit import text as nktext  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# 前端已从单个 app.js 拆成 ES Module（static/js/*.js）。
# 源码级断言仍然需要"一份完整的源码"，所以按文件名顺序拼回来——
# 所有断言都只跨相邻函数（同一模块内），因此拼接顺序不影响它们。
JS_DIR = ROOT / "webpanel" / "static" / "js"


def app_source() -> str:
    return "\n".join(
        (JS_DIR / name).read_text(encoding="utf-8")
        for name in sorted(p.name for p in JS_DIR.glob("*.js"))
    )


def strip_js_comments(raw: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


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


# ==========================================================================
# credentials：写 .env 必须安全
# ==========================================================================

ENV_SAMPLE = """# 我的配置
API_KEY=sk-existing-key

# 其它无关的键要原样保留
UNRELATED=keep-me
"""


class temp_env:
    """把 credentials.ENV_PATH 指向临时文件，并隔离进程内的同名环境变量。

    credentials.status() 会把 os.environ 当作回退来源（支持容器/CI 注入），
    所以测试期间必须清掉真实环境里的 API_KEY 等，否则本机 .env 会串进来。
    """

    _KEYS = ("API_KEY", "API_BASE_URL", "API_MODEL", "NOVELKIT_WORKSPACE")

    def __init__(self, content: str = ENV_SAMPLE):
        self.content = content

    def __enter__(self):
        self._saved = {key: os.environ.pop(key, None) for key in self._KEYS}
        self.dir = Path(tempfile.mkdtemp(prefix="panel_env_"))
        creds.ENV_PATH = self.dir / ".env"
        if self.content is not None:
            creds.ENV_PATH.write_text(self.content, encoding="utf-8")
        return creds.ENV_PATH

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)


def test_optional_model_keys_do_not_break_all_set() -> None:
    """新增的可选配置项（API 地址 / 模型）留空时不该把 all_set 拖成 false。

    它们有内置默认值，"留空"是合法状态；all_set 只反映必填项（只有 API Key）。
    """
    env_text = f"{creds.API_KEY}=sk-test\n"
    with temp_env(env_text):
        info = creds.status()
        expect("API_BASE_URL" in info["keys"] and "API_MODEL" in info["keys"],
               "可选键应出现在 status 里")
        expect(info["keys"]["API_BASE_URL"]["optional"] is True)
        expect(info["keys"]["API_BASE_URL"]["default"].startswith("http"),
               "可选项要带上内置默认值供面板显示")
        expect(info["all_set"] is True, f"all_set 应只看必填项: {info['all_set']}")

        # 可选项可以直接回显（不是密钥），且能保存与清空
        saved = creds.save({"API_BASE_URL": "https://my.api/v1", "API_MODEL": "m1"})
        expect(saved["ok"], str(saved))
        after = creds.status()
        expect(after["keys"]["API_BASE_URL"]["value"] == "https://my.api/v1", str(after["keys"]))
        expect(after["keys"]["API_MODEL"]["value"] == "m1")
        expect(after["keys"]["API_KEY"]["configured"] is True, "Key 仍只报是否配置")

        creds.save({"API_BASE_URL": "", "API_MODEL": ""})
        cleared = creds.status()
        expect(cleared["keys"]["API_BASE_URL"]["configured"] is False, "清空 = 回到默认")


def test_credentials_all_set_gates_translation() -> None:
    """前端要在点「翻译本章」时先判断 Key 是否已配置，依据就是 all_set / keys.API_KEY。"""
    with temp_env("# 空配置\nUNRELATED=keep\n"):
        info = creds.status()
        expect(info["all_set"] is False, str(info))
        expect(info["keys"]["API_KEY"]["configured"] is False, str(info))

    with temp_env("API_KEY=sk-abc\n"):
        info = creds.status()
        expect(info["all_set"] is True, str(info))
        expect(info["keys"]["API_KEY"]["configured"] is True, str(info))


def test_credentials_save_preserves_other_lines() -> None:
    """保存只替换目标键那一行：注释、空行、无关键必须原样保留。"""
    with temp_env() as env_path:
        before = env_path.read_text(encoding="utf-8")
        result = creds.save({"API_KEY": "sk-new-key"})
        expect(result["ok"], str(result))
        after = env_path.read_text(encoding="utf-8")

        expect("API_KEY=sk-new-key" in after, "API_KEY 未更新")
        expect("API_KEY=sk-existing-key" not in after, "旧值应被替换")
        expect("UNRELATED=keep-me" in after, "无关键被改动了")
        expect("# 我的配置" in after, "注释被删了")
        expect(len(after.splitlines()) == len(before.splitlines()), "行数发生了变化")
        expect((env_path.parent / ".env.bak").exists(), "应生成 .env.bak 备份")


def test_credentials_save_appends_missing_key() -> None:
    with temp_env() as env_path:
        result = creds.save({"API_KEY": "sk-new"})
        expect(result["ok"], str(result))
        after = env_path.read_text(encoding="utf-8")
        expect(after.count("API_KEY=") == 1, f"API_KEY 重复: {after}")
        expect("API_KEY=sk-new" in after, after)


def test_credentials_save_strips_prefixed_key() -> None:
    with temp_env() as env_path:
        creds.save({"API_KEY": "API_KEY=sk-typed-too-much"})
        after = env_path.read_text(encoding="utf-8")
        expect("API_KEY=sk-typed-too-much" in after, after)
        expect("API_KEY=API_KEY=" not in after, after)


def test_credentials_status_never_leaks_values() -> None:
    with temp_env() as _:
        status = creds.status()
        blob = repr(status)
        expect("sk-existing-key" not in blob, "状态里泄漏了 API Key")
        expect(status["keys"][creds.API_KEY]["configured"] is True)
        expect(status["keys"][creds.API_KEY]["length"] == len("sk-existing-key"))


def test_credentials_dry_run_does_not_write() -> None:
    """dry-run 只解析不落盘——避免"验证解析"时误改真实 .env。"""
    with temp_env() as env_path:
        before = env_path.read_text(encoding="utf-8")
        result = creds.save({"API_KEY": "sk-dry-run", "dry_run": True})
        expect(result["ok"] and result.get("dry_run"), str(result))
        expect(result["would_update"] == [creds.API_KEY], str(result))
        expect(env_path.read_text(encoding="utf-8") == before, "dry-run 改动了 .env")
        expect(not (env_path.parent / ".env.bak").exists(), "dry-run 不应产生备份")


def test_credentials_save_rejects_garbage() -> None:
    with temp_env() as env_path:
        before = env_path.read_text(encoding="utf-8")
        result = creds.save({"unknown_key": "hello"})
        expect(result["ok"] is False, str(result))
        expect(env_path.read_text(encoding="utf-8") == before, "失败时不应改动 .env")
        # 空值同样拒绝，且不产生备份
        result = creds.save({"API_KEY": "   "})
        expect(result["ok"] is False, str(result))
        expect(env_path.read_text(encoding="utf-8") == before, "失败时不应改动 .env")


def test_credentials_verify_reports_missing_key_and_ok() -> None:
    """验证接口不能把面板打崩：缺 Key 给中文提示，成功时回一个不含密钥的消息。"""
    import novelkit.config as nkconfig
    import novelkit.llm as nkllm

    with temp_env("# 空配置\n"):
        saved = nkconfig.get_api_key(required=False)
        os.environ.pop("API_KEY", None)
        try:
            result = creds.verify_model_api()
            expect(result["ok"] is False and "API_KEY" in result["error"], str(result))
        finally:
            if saved:
                os.environ["API_KEY"] = saved

    with temp_env("API_KEY=sk-verify-test\n"):
        os.environ["API_KEY"] = "sk-verify-test"
        original = nkllm.create_client

        class _FakeModels:
            def list(self):
                return {"data": []}

        class _FakeClient:
            models = _FakeModels()

        nkllm.create_client = lambda **kwargs: _FakeClient()
        try:
            result = creds.verify_model_api()
        finally:
            nkllm.create_client = original
            os.environ.pop("API_KEY", None)
        expect(result["ok"] is True, str(result))
        expect("sk-verify-test" not in repr(result), "验证结果里泄漏了 Key")


# ==========================================================================
# EPUB 导入
# ==========================================================================

def _make_epub(path: Path) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("test-123")
    book.set_title("测试书")
    book.set_language("zh")

    chapters = []
    for index in range(6):
        item = epub.EpubHtml(title=f"第{index}章", file_name=f"chap_{index}.xhtml", lang="zh")
        body = "".join(f"<p>第{index}章 第{p}段内容，用来测试导入。</p>" for p in range(4))
        item.content = f"<html><body><h1>第{index}章 标题</h1>{body}</body></html>"
        book.add_item(item)
        chapters.append(item)

    book.toc = tuple(chapters)
    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)


def test_epub_scan_lists_items() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    try:
        src = tmp / "book.epub"
        _make_epub(src)
        result = epub_import.scan_bytes(src.read_bytes(), "book.epub")
        expect(result["ok"], str(result))
        expect(result["document_count"] >= 6, f"条目数异常: {result['document_count']}")
        real = [i for i in result["items"] if not i["empty"]]
        expect(len(real) >= 6, str(len(real)))
        expect(all(i["title"] for i in real), "每个条目都应有标题")
        expect(real[0]["preview"], "应有预览文本")
        # 这个夹具没有简介/封面之类的"前置页"，所以不该凭空建议一个简介条目
        expect(result["suggested_intro"] is None, str(result["suggested_intro"]))
        expect(result["suggested_body_start"] == 0, str(result["suggested_body_start"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_structured_epub(path: Path) -> None:
    """构造和你手上那本同构的 EPUB：书名页 / 简介 / 目录 / 第01-03章 / 后记。"""
    import zipfile

    chapters = [
        ("s0", "赛博朋克：我在玩地球OL", "书名：赛博朋克：我在玩地球OL"),
        ("s1", "简介", "简介\n这是一段作品简介。"),
        ("s2", "目录", "目录\n第一章 ………… 1"),
        ("s3", "第01章 免费角色", "第01章 免费角色\n正文。"),
        ("s4", "第02章 赛博精神病", "第02章 赛博精神病\n正文。"),
        ("s5", "第03章 付费选项", "第03章 付费选项\n正文。"),
        ("s6", "后记", "后记\n作者的话。"),
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest = "".join(f'<item id="{i}" href="{i}.xhtml" media-type="application/xhtml+xml"/>'
                           for i, _, _ in chapters)
        spine = "".join(f'<itemref idref="{i}"/>' for i, _, _ in chapters)
        z.writestr("OEBPS/content.opf",
            f'<?xml version="1.0"?><package version="3.0" unique-identifier="i" '
            f'xmlns="http://www.idpf.org/2007/opf"><metadata '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">demo</dc:identifier>'
            f'<dc:title>赛博朋克：我在玩地球OL</dc:title><dc:language>zh</dc:language></metadata>'
            f'<manifest>{manifest}<item id="nav" href="nav.xhtml" '
            f'media-type="application/xhtml+xml"/></manifest><spine>{spine}</spine></package>')
        for i, title, body in chapters:
            paras = "".join(f"<p>{line}</p>" for line in body.splitlines())
            z.writestr(f"OEBPS/{i}.xhtml",
                       f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{title}</h1>'
                       f'{paras}</body></html>')
        z.writestr("OEBPS/nav.xhtml",
                   '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                   '<nav epub:type="toc"><ol></ol></nav></body></html>')


def test_epub_scan_classifies_entries() -> None:
    """条目分类：数字章节可识别，目录/前置页不能算章节。"""
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_cls_"))
    try:
        src = tmp / "cp.epub"
        _make_structured_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), src.name)
        by_title = {it["title"]: it for it in scan["items"]}

        # 目录：标题命中 + 文件名命中（nav.xhtml）
        expect(by_title["目录"]["toc"] is True, "「目录」应被判为目录")
        expect(any(it["toc"] and it["chapter_number"] is None
                   for it in scan["items"] if "nav" in it["name"]),
               "nav 文档应被判为目录")
        # 数字章节：第01章 / 第1章 都要认，序号取出来
        expect(by_title["第01章 免费角色"]["chapter_number"] == 1, str(by_title["第01章 免费角色"]))
        expect(by_title["第02章 赛博精神病"]["chapter_number"] == 2)
        expect(by_title["第03章 付费选项"]["chapter_number"] == 3)
        # 不做"特殊标题"猜测：只有目录会被标记，其余交给用户勾选
        for item in scan["items"]:
            expect("front_matter" not in item, "不应再有 front_matter 猜测字段")
        expect(by_title["简介"]["chapter_number"] is None)
        expect(by_title["赛博朋克：我在玩地球OL"]["chapter_number"] is None,
               "书名页解析不出章节号")
        # 番外这类特殊标题既不是目录、也不该被"特殊标题规则"误判
        expect(by_title["后记"]["chapter_number"] is None, "后记解析不出章节号")
        expect(by_title["后记"]["toc"] is False, "后记不是目录")

        # 建议值：简介是正文前最后一块前置页；正文起点在简介之后
        expect(scan["suggested_intro"] == by_title["简介"]["index"],
               f"建议简介不对: {scan['suggested_intro']}")
        expect(scan["suggested_body_start"] == by_title["第01章 免费角色"]["index"],
               f"建议正文起点不对: {scan['suggested_body_start']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_epub_commit_intro_is_not_written_as_body() -> None:
    """勾选「作为简介章节导入」时，那一条只写 0_origin.txt，不再当正文章节写一遍。"""
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_intro_"))
    original_root = epub_import._WORKSPACE_ROOT
    work_name = "epub-intro-test"
    try:
        epub_import._WORKSPACE_ROOT = tmp
        src = tmp / "cp.epub"
        _make_structured_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), src.name)
        intro = scan["suggested_intro"]
        numbered = [it["index"] for it in scan["items"] if it["chapter_number"] is not None]
        expect(numbered, "应识别出编号章节")

        # 故意把简介也塞进 body_indices（模拟旧前端/直接调接口）
        result = epub_import.commit(scan["scan_id"], work_name, include_intro=True,
                                    intro_index=intro,
                                    body_indices=[intro] + numbered, start_number=1)
        expect(result["ok"], str(result))
        expect(result["skipped_intro"] is True, "应报告简介被从正文里剔除")
        work = tmp / work_name
        expect((work / "0_origin.txt").exists(), "简介应写成 0_origin.txt")
        expect((work / "1_origin.txt").read_text(encoding="utf-8").startswith("第01章"),
               "第 1 章应是第01章（简介没有占掉编号）")
        expect(not (work / "4_origin.txt").exists(), "不应多出一个把简介写成第四章的文件")
        expect(result["missing_chapters"] == [], f"原书章节号不该有缺口: {result['missing_chapters']}")

        # 漏选一章时应报告缺口
        result2 = epub_import.commit(scan["scan_id"], work_name, include_intro=False,
                                     intro_index=None, body_indices=[numbered[0], numbered[2]],
                                     start_number=1, overwrite=True)
        expect(result2["ok"], str(result2))
        expect(result2["missing_chapters"] == [2],
               f"勾选里漏了第 2 章，应报告出来: {result2['missing_chapters']}")
    finally:
        epub_import._WORKSPACE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)


def test_epub_commit_selection_and_intro() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    project = Path(tempfile.mkdtemp(prefix="panel_proj_"))
    original_root = epub_import._WORKSPACE_ROOT
    try:
        src = tmp / "book.epub"
        _make_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), "book.epub")
        real = [i for i in scan["items"] if not i["empty"]]
        epub_import._WORKSPACE_ROOT = project

        body = [real[2]["index"], real[4]["index"]]  # 只挑两章
        result = epub_import.commit(
            scan["scan_id"], "newbook",
            include_intro=True, intro_index=real[1]["index"],
            body_indices=body, start_number=1, overwrite=False,
        )
        expect(result["ok"], str(result))
        expect(result["count"] == 3, f"应写入 1 简介 + 2 正文，实际 {result['count']}")

        work = project / "newbook"
        expect((work / "0_origin.txt").exists(), "简介未写入 0_origin.txt")
        expect((work / "1_origin.txt").exists(), "第 1 章未写入")
        expect((work / "2_origin.txt").exists(), "第 2 章未写入")
        expect(not (work / "3_origin.txt").exists(), "未选中的章节不应写入")

        text = (work / "1_origin.txt").read_text(encoding="utf-8")
        expect(real[2]["title"][:6] in text, "写入内容与所选条目不符")
        expect("<p>" not in text, "不应残留 HTML 标签")
    finally:
        epub_import._WORKSPACE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)


def test_epub_commit_intro_optional_and_start_number() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    project = Path(tempfile.mkdtemp(prefix="panel_proj_"))
    original_root = epub_import._WORKSPACE_ROOT
    try:
        src = tmp / "book.epub"
        _make_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), "book.epub")
        real = [i for i in scan["items"] if not i["empty"]]
        epub_import._WORKSPACE_ROOT = project

        result = epub_import.commit(
            scan["scan_id"], "nb2",
            include_intro=False, intro_index=None,
            body_indices=[real[2]["index"], real[3]["index"]],
            start_number=10, overwrite=False,
        )
        expect(result["ok"], str(result))
        work = project / "nb2"
        expect(not (work / "0_origin.txt").exists(), "未勾选简介时不应写 0_origin.txt")
        expect((work / "10_origin.txt").exists() and (work / "11_origin.txt").exists(),
               f"起始章号未生效: {sorted(p.name for p in work.iterdir())}")
    finally:
        epub_import._WORKSPACE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)


def test_epub_commit_refuses_overwrite_by_default() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    project = Path(tempfile.mkdtemp(prefix="panel_proj_"))
    original_root = epub_import._WORKSPACE_ROOT
    try:
        src = tmp / "book.epub"
        _make_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), "book.epub")
        real = [i for i in scan["items"] if not i["empty"]]
        epub_import._WORKSPACE_ROOT = project

        args = dict(include_intro=False, intro_index=None,
                    body_indices=[real[2]["index"]], start_number=1)
        first = epub_import.commit(scan["scan_id"], "nb3", overwrite=False, **args)
        expect(first["ok"], str(first))

        (project / "nb3" / "1_origin.txt").write_text("用户手改过的内容", encoding="utf-8")
        second = epub_import.commit(scan["scan_id"], "nb3", overwrite=False, **args)
        expect(second["ok"] is False, "默认应拒绝覆盖已有文件")
        expect("已存在" in second["error"] or "覆盖" in second["error"], second["error"])
        expect((project / "nb3" / "1_origin.txt").read_text(encoding="utf-8") == "用户手改过的内容",
               "拒绝覆盖后文件仍被改动了")

        third = epub_import.commit(scan["scan_id"], "nb3", overwrite=True, **args)
        expect(third["ok"], str(third))
    finally:
        epub_import._WORKSPACE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)


def test_epub_commit_rejects_bad_work_name() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="panel_epub_"))
    original_root = epub_import._WORKSPACE_ROOT
    try:
        epub_import._WORKSPACE_ROOT = tmp
        src = tmp / "book.epub"
        _make_epub(src)
        scan = epub_import.scan_bytes(src.read_bytes(), "book.epub")
        for bad in ("../escape", "a/b", "", "  ", ".hidden", "..", "x" * 80,
                    "bad:name", "bad\\name"):
            result = epub_import.commit(scan["scan_id"], bad, include_intro=False,
                                        intro_index=None, body_indices=[1])
            expect(result["ok"] is False, f"应拒绝非法作品名 {bad!r}")

        # 中文书名必须能用：作品名就是目录名，清洗成下划线是曾经的 bug
        chinese = "赛博朋克：我在玩地球OL"
        ok = epub_import.commit(scan["scan_id"], chinese, include_intro=False,
                                intro_index=None, body_indices=[1], start_number=1)
        expect(ok["ok"], f"中文作品名应被接受: {ok}")
        expect((tmp / chinese / "1_origin.txt").exists(),
               "应在中文作品目录下写出章节")
    finally:
        epub_import._WORKSPACE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 段落对齐
# ==========================================================================

ALIGN_ZH_LINES = [
    "第1章 测试章节",
    "【剩余人数：10000】",
    "他抬头看了一眼聊天框左上角的名字。",
    "风从山脊上吹下来，带着雪粒。",
    "他没有回答，只是把手按在了剑柄上。",
]
# 英文故意把中间三段并成两段：正确的对齐必须是 ZH[1:3] ↔ EN[1]、ZH[3:5] ↔ EN[2]，
# 只要按下标逐段配就会整体串行一格（这正是当初报的 bug）。
ALIGN_EN_LINES = [
    "Chapter 1: Test Chapter",
    "[Remaining Population: 10,000] He glanced at the name in the top-left corner of the chat.",
    "Wind came down off the ridge, carrying sleet. He did not answer, only put his hand on the sword hilt.",
]


def _make_alignment_work() -> Path:
    work = Path(tempfile.mkdtemp(prefix="panel_align_"))
    (work / "1_origin.txt").write_text("\n".join(ALIGN_ZH_LINES) + "\n", encoding="utf-8")
    (work / "1_translated.txt").write_text("\n".join(ALIGN_EN_LINES) + "\n", encoding="utf-8")
    return work


def test_alignment_fixes_the_reported_drift() -> None:
    """合并段落处的对齐：中文 5 段 / 英文 3 段，必须配对正确而不是整体串行。"""
    work = _make_alignment_work()
    try:
        data = lib.read_chapter(work, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    pairs = {tuple(r["zh_parts"]): r["en_parts"] for r in data["rows"]}
    pairs = {tuple(r["zh_parts"]): r["en_parts"] for r in data["rows"]}

    remaining = [k for k in pairs if any("剩余人数" in p for p in k)]
    expect(remaining, "没有找到「剩余人数」段落")
    expect("Remaining Population" in " ".join(pairs[remaining[0]]),
           f"「剩余人数」配对错误: {pairs[remaining[0]]}")

    chat = [k for k in pairs if any("左上角" in p for p in k)]
    expect(chat, "没有找到「左上角」段落")
    expect("top-left corner" in " ".join(pairs[chat[0]]),
           f"「左上角」配对错误: {pairs[chat[0]]}")

    # 合并点：第二、三段中文一起配到第一段英文（而不是各配各的）
    merged = [k for k in pairs if len(k) == 2 and any("聊天框" in p for p in k)]
    expect(merged, f"没有识别出合并段落: {[list(k) for k in pairs]}")
    expect(len(pairs[merged[0]]) == 1, f"合并行英文侧应只有 1 段: {pairs[merged[0]]}")


def test_alignment_covers_everything_exactly_once() -> None:
    """不丢不重：两侧每个段落都必须出现且仅出现一次（含合并/单侧缺失的极端情况）。"""
    work = Path(tempfile.mkdtemp(prefix="panel_align_cover_"))
    try:
        cases = {
            1: (["甲", "乙", "丙"], ["A", "B", "C"]),                 # 1:1
            2: (["甲", "乙", "丙"], ["A B", "C"]),                    # 中文多、英文合并
            3: (["甲", "乙"], ["A", "B", "C"]),                       # 英文多、中文合并
            4: (["甲", "乙", "丙", "丁"], ["A", "B C D"]),             # 2:1 与 3:1 混排
            5: (["甲", "乙", "丙"], ["A", "B", "C", "D", "E"]),        # 英文拆得更碎
        }
        for number, (zh_lines, en_lines) in cases.items():
            (work / f"{number}_origin.txt").write_text("\n".join(zh_lines) + "\n",
                                                       encoding="utf-8")
            (work / f"{number}_translated.txt").write_text("\n".join(en_lines) + "\n",
                                                           encoding="utf-8")
            data = lib.read_chapter(work, number)
            got_zh = [p for r in data["rows"] for p in r["zh_parts"]]
            got_en = [p for r in data["rows"] for p in r["en_parts"]]
            expect(got_zh == zh_lines, f"{number} 中文覆盖异常: {got_zh}")
            expect(got_en == en_lines, f"{number} 英文覆盖异常: {got_en}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_alignment_handles_pathological_input() -> None:
    expect(lib.align_paragraphs([], []) == [])
    rows = lib.align_paragraphs(["甲"], [])
    expect(len(rows) == 1 and rows[0]["gap"] == "en" and rows[0]["en"] is None)
    rows = lib.align_paragraphs([], ["A"])
    expect(len(rows) == 1 and rows[0]["gap"] == "zh" and rows[0]["zh"] is None)
    # 长度差异极大也不应崩溃
    rows = lib.align_paragraphs(["短"], ["x" * 4000])
    expect(len(rows) >= 1)


def _temp_work() -> Path:
    """建一个最小的作品目录（含 1_origin.txt / 1_translated.txt）。"""
    work = Path(tempfile.mkdtemp(prefix="panel_work_"))
    (work / "1_origin.txt").write_text("第一段中文。\n第二段中文。\n", encoding="utf-8")
    (work / "1_translated.txt").write_text("First paragraph.\n", encoding="utf-8")
    (work / "2_origin.txt").write_text("只有原文。\n", encoding="utf-8")
    return work


def test_editor_save_backs_up_and_is_atomic() -> None:
    """保存必须先把旧内容备份下来，再原子替换；返回值要能反映改了什么。"""
    work = _temp_work()
    try:
        result = editor_service.save_text(work, 1, "translated", "Rewritten.\n")
        expect(result["ok"], str(result))
        expect(result["changed"] is True, str(result))
        expect((work / "1_translated.txt").read_text(encoding="utf-8") == "Rewritten.\n")
        backup = work / result["backup"]
        expect(result["backup"].startswith(".backups/"), result["backup"])
        expect(backup.exists(), "备份文件不存在")
        expect(backup.read_text(encoding="utf-8") == "First paragraph.\n", "备份内容不对")
        expect(result["chars"] == len("Rewritten.\n"), str(result))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_editor_creates_new_chapter_and_reports_unchanged() -> None:
    work = _temp_work()
    try:
        created = editor_service.save_text(work, 3, "origin", "新的一章。\n")
        expect(created["created"] is True, str(created))
        expect(created["backup"] == "", "没有旧文件时不应产生备份")
        expect((work / "3_origin.txt").exists())

        again = editor_service.save_text(work, 3, "origin", "新的一章。\n")
        expect(again["ok"] and again["changed"] is False, str(again))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_editor_create_refuses_to_overwrite_existing() -> None:
    """「新建章节」撞上已有文件必须拒绝，别把现成译文清空。"""
    work = _temp_work()
    try:
        try:
            editor_service.save_text(work, 1, "translated", "", create=True)
        except editor_service.EditorConflict as exc:
            expect("已经存在" in str(exc), str(exc))
        else:
            raise AssertionError("已存在的章节不应允许 create=True 覆盖")
        expect((work / "1_translated.txt").read_text(encoding="utf-8") == "First paragraph.\n",
               "被拒绝的写入不应改动文件")
        # 正常保存（create=False）仍然允许覆盖
        editor_service.save_text(work, 1, "translated", "Changed.\n")
        expect((work / "1_translated.txt").read_text(encoding="utf-8") == "Changed.\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_editor_delete_chapter_backs_up() -> None:
    """删除本章：备份后删掉这一章的所有衍生文件，不碰其它章。"""
    work = Path(tempfile.mkdtemp(prefix="panel_del_"))
    try:
        (work / "1_origin.txt").write_text("第1章", encoding="utf-8")
        (work / "1_translated.txt").write_text("Chapter 1", encoding="utf-8")
        (work / "1_refine.json").write_text("{}", encoding="utf-8")
        (work / "glossary_1.json").write_text('{"fixed_terms": {}}', encoding="utf-8")
        (work / "2_origin.txt").write_text("第2章", encoding="utf-8")

        res = editor_service.delete_chapter(work, 1)
        expect(res["ok"], str(res))
        expect(res["count"] == 4, f"应删掉 4 个文件: {res}")
        expect(len(res["backups"]) == 4, "每个文件都应留备份")
        expect(not (work / "1_origin.txt").exists(), "第 1 章原文应被删除")
        expect(not (work / "glossary_1.json").exists(), "本章术语库也应一并删掉")
        expect((work / "2_origin.txt").exists(), "不该动第 2 章")
        expect(res["backups"][0].startswith(".backups/"), str(res["backups"]))
        expect((work / res["backups"][0]).exists(), "备份文件应存在")

        # 删不存在的章：明确报错，不静默成功
        try:
            editor_service.delete_chapter(work, 9)
        except editor_service.EditorError as exc:
            expect("没有可删除" in str(exc), str(exc))
        else:
            raise AssertionError("删除不存在的章节应当报错")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_work_rename_and_delete() -> None:
    """作品目录级操作：改名要搬目录，删除是移入 .trash/（可恢复）。"""
    root = Path(tempfile.mkdtemp(prefix="works_op_"))
    try:
        work = root / "旧名"
        work.mkdir()
        (work / "1_origin.txt").write_text("第1章", encoding="utf-8")
        (work / "1_translated.txt").write_text("Chapter 1", encoding="utf-8")

        res = works_service.rename(work, "新名", root=root)
        expect(res["ok"] and res["work"] == "新名", str(res))
        expect(not work.exists(), "旧目录应该没了")
        renamed = root / "新名"
        expect((renamed / "1_origin.txt").read_text(encoding="utf-8") == "第1章",
               "改名后内容要完整")
        expect((renamed / "1_translated.txt").exists(), "译文也要跟着走")

        # 非法名/重名都要拒绝，且不动数据
        for bad in ("", "  ", ".hidden", "a/b", "x" * 80):
            try:
                works_service.rename(renamed, bad, root=root)
            except works_service.WorkError:
                pass
            else:
                raise AssertionError(f"应拒绝非法作品名 {bad!r}")
        (root / "占位").mkdir()
        (root / "占位" / "1_origin.txt").write_text("x", encoding="utf-8")
        try:
            works_service.rename(renamed, "占位", root=root)
        except works_service.WorkError as exc:
            expect("已经存在" in str(exc), str(exc))
        else:
            raise AssertionError("重名应被拒绝")
        expect((renamed / "1_origin.txt").exists(), "拒绝后原目录必须完好")

        # 删除 = 移入 .trash/（带时间戳），文件一个不少
        deleted = works_service.delete(renamed, root=root)
        expect(deleted["ok"], str(deleted))
        expect(not renamed.exists(), "原目录应已移走")
        trash_target = root / deleted["trash_path"]
        expect(trash_target.is_dir(), f"回收站里应有它: {deleted['trash_path']}")
        expect((trash_target / "1_origin.txt").exists(), "回收站里内容完整")
        expect(deleted["files"] >= 2, str(deleted))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_editor_rejects_bad_side_and_negative_number() -> None:
    work = _temp_work()
    try:
        for side, num in (("sideways", 1), ("translated", -1)):
            try:
                editor_service.save_text(work, num, side, "x")
            except editor_service.EditorError:
                pass
            else:
                raise AssertionError(f"应拒绝 side={side} num={num}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_editor_inventory_lists_missing_sides() -> None:
    work = _temp_work()
    try:
        info = editor_service.inventory(work)
        expect(info["numbers"] == [1, 2], str(info))
        expect(info["missing_translated"] == [2], str(info))
        expect(info["missing_origin"] == [], str(info))
        expect(info["next_number"] == 3, str(info))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ==========================================================================
# 后台任务
# ==========================================================================

def test_job_lifecycle_and_log() -> None:
    original_dir = jobs_service._JOB_DIR
    jobs_service._JOB_DIR = Path(tempfile.mkdtemp(prefix="panel_jobs_"))
    jobs_service._JOBS.clear()
    jobs_service._ORDER.clear()
    try:
        script = "import time; print('hello-from-job'); time.sleep(0.2); print('done')"
        started = jobs_service.start("test", [sys.executable, "-c", script], label="测试任务")
        expect(started["ok"], str(started))
        job_id = started["job"]["id"]

        # 互斥：同一时刻只允许一个任务
        second = jobs_service.start("test", [sys.executable, "-c", "print(1)"])
        expect(second["ok"] is False, "应拒绝并发任务")

        for _ in range(60):
            job = jobs_service.get(job_id)
            if job["state"] != "running":
                break
            time.sleep(0.1)
        job = jobs_service.get(job_id)
        expect(job["state"] == "done", f"任务状态异常: {job['state']}")
        expect(job["returncode"] == 0)
        expect("hello-from-job" in job["log"], f"日志未捕获: {job['log']!r}")
        expect("done" in job["log"])

        # 结束后可以再起一个
        third = jobs_service.start("test", [sys.executable, "-c", "print('again')"])
        expect(third["ok"], "任务结束后应能再次启动")
    finally:
        jobs_service._JOB_DIR = original_dir
        jobs_service._JOBS.clear()
        jobs_service._ORDER.clear()


def test_job_failure_and_cancel() -> None:
    original_dir = jobs_service._JOB_DIR
    jobs_service._JOB_DIR = Path(tempfile.mkdtemp(prefix="panel_jobs_"))
    jobs_service._JOBS.clear()
    jobs_service._ORDER.clear()
    try:
        failed = jobs_service.start("test", [sys.executable, "-c", "import sys; sys.exit(3)"])
        job_id = failed["job"]["id"]
        for _ in range(60):
            if jobs_service.get(job_id)["state"] != "running":
                break
            time.sleep(0.1)
        expect(jobs_service.get(job_id)["state"] == "failed")
        expect(jobs_service.get(job_id)["returncode"] == 3)

        long_job = jobs_service.start("test", [sys.executable, "-c", "import time; time.sleep(30)"])
        busy_id = long_job["job"]["id"]
        time.sleep(0.4)
        cancelled = jobs_service.cancel(busy_id)
        expect(cancelled["ok"], str(cancelled))
        for _ in range(60):
            if jobs_service.get(busy_id)["state"] not in ("running",):
                break
            time.sleep(0.1)
        expect(jobs_service.get(busy_id)["state"] == "cancelled")
        # 回归：进程随后真的退出（码 -15）时，收尾线程不得把"已取消"盖成"失败"
        time.sleep(0.6)
        expect(jobs_service.get(busy_id)["state"] == "cancelled",
               f"取消状态被覆盖: {jobs_service.get(busy_id)['state']}")
    finally:
        jobs_service._JOB_DIR = original_dir
        jobs_service._JOBS.clear()
        jobs_service._ORDER.clear()


def test_job_stream_sse_pushes_incremental_output() -> None:
    """任务日志走 SSE：init 给历史、append 给增量、end 给终态。

    用"假任务"（只往 log_path 写文件 + 改 state）驱动，不启子进程，
    这样 init/append/end 三个事件都能稳定命中，不会有时序抖动。
    """
    import threading
    import urllib.error
    import urllib.request

    original_dir = jobs_service._JOB_DIR
    jobs_service._JOB_DIR = Path(tempfile.mkdtemp(prefix="panel_jobs_"))
    jobs_service._JOBS.clear()
    jobs_service._ORDER.clear()
    log_path = jobs_service._JOB_DIR / "stream.log"
    log_path.write_text("line-1\n", encoding="utf-8")
    job_id = "deadbeefcafe"
    jobs_service._JOBS[job_id] = {
        "id": job_id, "kind": "test", "label": "SSE 测试", "state": "running",
        "started": "2026-01-01 00:00:00", "ended": None, "returncode": None,
        "log_path": str(log_path), "argv": ["true"], "_process": None, "_handle": None,
    }
    jobs_service._ORDER.append(job_id)

    httpd, base = _start_panel()
    collected: list = []
    failures: list = []

    def read_stream() -> None:
        try:
            req = urllib.request.Request(f"{base}/api/jobs/{job_id}/stream")
            with urllib.request.urlopen(req, timeout=15) as res:
                expect(res.headers.get("Content-Type", "").startswith("text/event-stream"),
                       f"Content-Type 应为 text/event-stream: {res.headers.get('Content-Type')}")
                event = None
                for raw in res:
                    line = raw.decode("utf-8").rstrip("\n")
                    if line.startswith("event: "):
                        event = line[len("event: "):]
                    elif line.startswith("data: "):
                        collected.append((event, json.loads(line[len("data: "):])))
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    try:
        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        time.sleep(0.6)                      # 建连并收到 init
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("line-2\n")
        time.sleep(0.6)                      # 让 append 推出来
        jobs_service._JOBS[job_id]["returncode"] = 0
        jobs_service._JOBS[job_id]["state"] = "done"
        reader.join(timeout=10)

        expect(not failures, f"SSE 读取失败: {failures}")
        kinds = [kind for kind, _ in collected]
        expect(kinds and kinds[0] == "init", f"首个事件应是 init: {kinds}")
        expect("append" in kinds, f"应有 append 增量: {kinds}")
        expect(kinds[-1] == "end", f"最后应是 end: {kinds}")
        expect("line-1" in collected[0][1]["log"], "init 应带已有日志")
        merged = collected[0][1]["log"] + "".join(
            payload["chunk"] for kind, payload in collected if kind == "append")
        expect("line-2" in merged, f"append 应带新增输出: {merged!r}")
        expect(collected[-1][1]["job"]["state"] == "done", "end 应带终态任务信息")

        # 任务不存在时不应挂成长连接，而是普通 404 JSON
        status, payload = _request(f"{base}/api/jobs/{'0' * 12}/stream", "GET")
        expect(status == 404 and payload.get("ok") is False,
               f"不存在的任务应返回 404: {status} {payload}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs_service._JOB_DIR = original_dir
        jobs_service._JOBS.clear()
        jobs_service._ORDER.clear()



# ==========================================================================
# 局部重译：回填必须"只动选中的行"
# ==========================================================================

def _fake_rows():
    """构造 4 行的对齐结果：1:1、1:1、2:1（合并）、1:2（拆分）。"""
    return [
        {"zh_parts": ["第一段"], "en_parts": ["First."], "gap": None},
        {"zh_parts": ["第二段"], "en_parts": ["Second."], "gap": None},
        {"zh_parts": ["第三段上", "第三段下"], "en_parts": ["Third merged."], "gap": None},
        {"zh_parts": ["第四段"], "en_parts": ["Fourth A.", "Fourth B."], "gap": None},
    ]


def test_merge_translations_only_touches_selected_rows() -> None:
    import retranslate

    rows = _fake_rows()
    en = [p for row in rows for p in row["en_parts"]]
    original = list(en)

    # 只重译第 2 行
    new_en, replaced = retranslate.merge_translations(rows, en, [(2, rows[1])], {2: "Second revised."})
    expect(replaced == 1, f"应只替换 1 段，实际 {replaced}")
    expect(new_en[1] == "Second revised.", str(new_en))
    expect(len(new_en) == len(original), "段落数不应变化")
    for i in (0, 2, 3):
        expect(new_en[i] == original[i], f"第 {i} 段被误改：{new_en[i]!r}")


def test_merge_translations_handles_merged_row() -> None:
    """2 段中文合并成 1 段英文：重译后应整体替换那 1 段。"""
    import retranslate

    rows = _fake_rows()
    en = [p for row in rows for p in row["en_parts"]]
    new_en, replaced = retranslate.merge_translations(
        rows, en, [(3, rows[2])], {3: "Third rewritten."})
    expect(replaced == 1)
    expect(new_en[2] == "Third rewritten.", str(new_en))
    # 原本 5 段英文（1+1+1+2），合并行是 1 段，替换后仍为 5 段
    expect(len(new_en) == 5, f"合并行应仍是 1 段英文: {new_en}")
    expect(new_en[0] == "First." and new_en[1] == "Second.")


def test_merge_translations_handles_split_row() -> None:
    """1 段中文拆成 2 段英文：重译后用 1 段替换那 2 段。"""
    import retranslate

    rows = _fake_rows()
    en = [p for row in rows for p in row["en_parts"]]
    expect(len(en) == 5, str(en))
    new_en, replaced = retranslate.merge_translations(
        rows, en, [(4, rows[3])], {4: "Fourth rewritten."})
    expect(replaced == 1)
    expect(len(new_en) == 4, f"拆分行应合并为 1 段: {new_en}")
    expect(new_en[3] == "Fourth rewritten.", str(new_en))


def test_merge_translations_skips_rows_without_model_output() -> None:
    """模型没返回某行时必须保持原样，不能把那段删掉。"""
    import retranslate

    rows = _fake_rows()
    en = [p for row in rows for p in row["en_parts"]]
    new_en, replaced = retranslate.merge_translations(
        rows, en, [(1, rows[0]), (2, rows[1])], {1: "First revised."})
    expect(replaced == 1, f"只有 1 行有输出，实际替换 {replaced}")
    expect(new_en == ["First revised.", "Second.", "Third merged.", "Fourth A.", "Fourth B."],
           str(new_en))


def test_merge_translations_normalises_punctuation() -> None:
    import retranslate

    rows = _fake_rows()
    en = [p for row in rows for p in row["en_parts"]]
    new_en, _ = retranslate.merge_translations(rows, en, [(1, rows[0])], {1: "他说：“好”。"})
    expect("“" not in new_en[0] and "。" not in new_en[0], f"标点未规范化: {new_en[0]!r}")


def test_retranslate_parse_rows() -> None:
    import retranslate

    expect(retranslate.parse_rows("3,7-9") == [3, 7, 8, 9])
    expect(retranslate.parse_rows("5") == [5])
    expect(retranslate.parse_rows("9-7") == [7, 8, 9], "区间应自动排序")
    expect(retranslate.parse_rows("1,1,2") == [1, 2], "应去重")
    expect(retranslate.parse_rows("") == [])


def test_merge_translations_consecutive_rows_keep_added_paragraph() -> None:
    """相邻两行一起重译时，前一行补回来的段落不能被后一行覆盖掉。

    真实场景（某章第 67 行）：中文"系统？"被错配到下一行，
    英文漏译了它。用户勾选 67、68 两段重译后，"系统？"的译文正好落在
    68 行的下标区间里，被 68 行原地覆盖 —— 补回来的"System?"又消失了。
    """
    import retranslate

    rows = [
        {"zh_parts": ["上一段"], "en_parts": ["Previous."], "gap": None},
        {"zh_parts": ["这一段", "系统？"], "en_parts": ["This one."], "gap": None},
        {"zh_parts": ["叮？"], "en_parts": ["Ding?"], "gap": None},
    ]
    en = ["Previous.", "This one.", "Ding?"]
    new_en, replaced = retranslate.merge_translations(
        rows, en, [(2, rows[1]), (3, rows[2])],
        {2: 'This one.\n\n"System?"', 3: '"Ding?"'})

    expect(replaced == 2, f"两行都应回填，实际 {replaced}")
    expect(new_en == ["Previous.", "This one.", '"System?"', '"Ding?"'], str(new_en))
    expect(any("System?" in p for p in new_en), "补回来的段落不能消失")


def test_merge_translations_gap_then_next_row_keeps_source() -> None:
    """先补译一个没有英文的行（gap），再重译下一行：下一行原有的英文不能被吞掉。"""
    import retranslate

    rows = [
        {"zh_parts": ["甲"], "en_parts": ["A"], "gap": None},
        {"zh_parts": ["乙"], "en_parts": [], "gap": "en"},
        {"zh_parts": ["丙"], "en_parts": ["C"], "gap": None},
    ]
    # 只补译 gap 行，未选中的 C 必须原样保留
    new_en, _ = retranslate.merge_translations(rows, ["A", "C"], [(2, rows[1])], {2: "B new"})
    expect(new_en == ["A", "B new", "C"], str(new_en))

    # gap 行与相邻行一起重译，顺序应为 甲 乙 丙
    new_en2, replaced2 = retranslate.merge_translations(
        rows, ["A", "C"], [(1, rows[0]), (2, rows[1]), (3, rows[2])],
        {1: "A new", 2: "B new", 3: "C new"})
    expect(replaced2 == 3, str(replaced2))
    expect(new_en2 == ["A new", "B new", "C new"], str(new_en2))


# ==========================================================================
# 共享提示词模块（整章翻译与局部重译必须一致）
# ==========================================================================

def test_prompt_module_is_shared_and_consistent() -> None:
    from novelkit import prompt as nkprompt

    glossary = {"fixed_terms": {"主城": "Major Hub"}, "contextual_terms": {},
                "aesthetic_sentences": {}, "cultural_nuances": {}}

    sys_a, user_a = nkprompt.build_messages(
        "SYS", "正文", glossary, anchor="<a/>", prev_context="P", future_context="F")
    expect("【术语库】" in sys_a and "Major Hub" in sys_a, sys_a[:200])
    expect("<source_to_translate>\n正文\n</source_to_translate>" in user_a, user_a)
    expect("<previous>\nP\n  </previous>" in user_a, user_a)
    expect("<future>\nF\n  </future>" in user_a, user_a)
    expect(nkprompt.RAG_INSTRUCTION not in sys_a, "未提供检索块时不应注入 RAG 规则")
    sys_b, user_b = nkprompt.build_messages("SYS", "正文", glossary, research="<term_context_research/>")
    expect(nkprompt.RAG_INSTRUCTION in sys_b, "提供检索块时必须注入 RAG 规则")
    expect("<term_context_research/>" in user_b, user_b)


def test_partial_prompt_keeps_context_and_glossary() -> None:
    from novelkit import prompt as nkprompt

    glossary = {"fixed_terms": {"云舟": "Memory Retainer"}, "contextual_terms": {},
                "aesthetic_sentences": {}, "cultural_nuances": {}}
    segments = [{"row": 3, "text": "第三段"}, {"row": 7, "text": "第七段"}]
    system, user = nkprompt.build_partial_messages(
        "SYS", segments, glossary,
        anchor="<a/>", prev_context="前文", future_context="后文", research="<r/>")

    # 与整章翻译一致的注入
    expect("Memory Retainer" in system, "术语库未注入")
    expect(nkprompt.RAG_INSTRUCTION in system, "RAG 规则未注入")
    expect("<r/>" in user, "RAG 检索块未注入")
    expect("前文" in user and "后文" in user, "上下文未注入")
    expect("<a/>" in user, "风格基准未注入")
    # 段落编号
    expect("[3] 第三段" in user and "[7] 第七段" in user, user[-600:])
    # 必须要求按编号回填、且不得省略键
    expect("translations" in system, "未声明 translations 输出格式")
    expect("不得省略任何键" in user, "未强调键必须齐全")
    expect("未列出的段落不要出现" in user, "未禁止改动其它段落")


def test_main_uses_the_shared_prompt_module() -> None:
    """回归：scripts/translate.py 不得再自带一份消息组装（否则与局部重译会漂移）。"""
    source = (ROOT / "scripts" / "translate.py").read_text(encoding="utf-8")
    expect("nkprompt.build_messages" in source, "translate.py 未使用共享 build_messages")
    expect("RAG_INSTRUCTION = (" not in source, "translate.py 仍自带 RAG_INSTRUCTION 副本")


# ==========================================================================
# 术语提取（正文高亮用）
# ==========================================================================

def test_split_renderings_handles_multiple_and_rejects_junk() -> None:
    """术语译法要拆成多条（"A / B"），并丢弃中文/超长/句子型的值。"""
    split = lib._split_renderings

    expect(split("Special Effect / Effect") == ["Special Effect", "Effect"], str(split("Special Effect / Effect")))
    expect(split({"正式": "Tier", "口语": "Level"}) == ["Tier", "Level"], str(split({"正式": "Tier"})))
    expect(split("Paladin") == ["Paladin"])
    # 中文值必须忽略（提示词里也是这个规则）
    expect(split("骑士") == [], str(split("骑士")))
    expect(split({"x": "骑士"}) == [])
    # 超长解释不是译法
    long_text = "这是一段很长的解释" + "x" * 80
    expect(split(long_text) == [] or all(len(v) <= 60 for v in split(long_text)))
    expect(split(" ".join(["word"] * 12)) == [], "12 个词的长句应被丢弃")
    expect(split(None) == [])
    expect(split("") == [])
    # 去重
    expect(split("Effect / Effect") == ["Effect"])


TERM_CHAPTER_TEXT = (
    "第41章 测试\n"
    "林越走进主城，云舟跟在他身后。\n"
    "主城的大门紧闭着。\n"
)


def _make_terms_work() -> Path:
    """合成一章：正文里出现术语，并带一份章节术语库。"""
    work = Path(tempfile.mkdtemp(prefix="panel_terms_"))
    (work / "41_origin.txt").write_text(TERM_CHAPTER_TEXT, encoding="utf-8")
    nktext.write_json_atomic(str(work / "glossary_41.json"), {
        "fixed_terms": {"主城": "Major Hub", "云舟": "Cloudboat"},
        "contextual_terms": {"林越": "Lin Yue"},
    })
    return work


def test_chapter_terms_expose_english_renderings() -> None:
    """前端要按英文译法做高亮，因此接口必须给出 translations 列表。"""
    work = _make_terms_work()
    try:
        terms = lib.read_chapter(work, 41).get("terms") or []
    finally:
        shutil.rmtree(work, ignore_errors=True)
    expect(terms, "应提取到术语")
    with_en = [t for t in terms if t.get("translations")]
    expect(with_en, "至少要有术语带 English 译法")
    for item in with_en[:20]:
        expect(isinstance(item["translations"], list), str(item))
        expect("translation" in item, "仍需保留可展示的 translation 字段")
        for value in item["translations"]:
            expect(not re.search(r"[\u4e00-\u9fff]", value), f"译法不应含中文: {value!r}")


def test_chapter_terms_only_returns_terms_present_in_text() -> None:
    work = _make_terms_work()
    try:
        data = lib.read_chapter(work, 41)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    terms = data.get("terms") or []
    expect(terms, "应提取到术语")
    text = TERM_CHAPTER_TEXT
    for item in terms[:50]:
        expect(item["term"] in text, f"术语 {item['term']} 并未出现在正文里")
        expect(item["category"] in ("fixed_terms", "contextual_terms"), item["category"])
    # 本章新术语优先排在前面
    expect(terms[0]["category"] in ("fixed_terms", "contextual_terms"))



# ==========================================================================
# 补充要求（<user_supplement>）持久化
# ==========================================================================

def test_snippets_roundtrip_and_limits() -> None:
    original = snippets_service.SNIPPETS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="panel_snip_"))
    snippets_service.SNIPPETS_PATH = tmp / "prompt_snippets.json"
    try:
        got = snippets_service.get_all("demo-work")
        expect(got["snippets"] == {"translate": "", "retranslate": "", "refine": ""}, str(got))
        expect(len(got["kinds"]) == 3, "三种场合各一份")

        saved = snippets_service.save("demo-work", "translate", "语气活泼")
        expect(saved["ok"] and saved["snippets"]["translate"] == "语气活泼", str(saved))
        snippets_service.save("demo-work", "refine", "改动尽量小")
        got = snippets_service.get_all("demo-work")
        expect(got["snippets"]["translate"] == "语气活泼", str(got))
        expect(got["snippets"]["refine"] == "改动尽量小", str(got))
        expect(got["snippets"]["retranslate"] == "", "不同场合互不影响")

        # 清空后不应残留空字符串
        snippets_service.save("demo-work", "translate", "   ")
        expect(snippets_service.get_all("demo-work")["snippets"]["translate"] == "")

        expect(snippets_service.save("demo-work", "nope", "x")["ok"] is False, "未知类型应被拒绝")
        expect(snippets_service.save("demo-work", "translate", "x" * 9000)["ok"] is False, "超长应被拒绝")

        # materialise 落成文件才能安全地传给 CLI
        snippets_service.save("demo-work", "translate", "多行\n含引号 \" 与分号 ;")
        path = snippets_service.materialise("demo-work", "translate")
        expect(path and Path(path).exists(), "应生成临时文件")
        expect("含引号" in Path(path).read_text(encoding="utf-8"), "内容应完整写入")
        Path(path).unlink()
        expect(snippets_service.materialise("demo-work", "retranslate") == "", "空内容不生成文件")
    finally:
        snippets_service.SNIPPETS_PATH = original
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 术语人工录入：先到先得不能被绕过
# ==========================================================================

def _make_term_work() -> Path:
    work = Path(tempfile.mkdtemp(prefix="panel_term_"))
    nktext.write_json_atomic(str(work / "glossary_0.json"),
                             {"fixed_terms": {"主城": "Major Hub"}})
    nktext.write_json_atomic(str(work / "glossary_3.json"),
                             {"fixed_terms": {"石昊": "Shi Hao"}})
    (work / "0_origin.txt").write_text("主城", encoding="utf-8")
    return work


def test_glossary_add_new_term() -> None:
    work = _make_term_work()
    try:
        res = glossary_edit_service.add_term(work, None, term="云舟", translation="Memory Retainer",
                                             category="fixed_terms")
        expect(res["ok"] and res["action"] == "added", str(res))
        # 新词写入最新章（3）
        expect(res["chapter"] == 3, str(res))
        saved = json.loads((work / "glossary_3.json").read_text(encoding="utf-8"))
        expect(saved["fixed_terms"]["云舟"] == "Memory Retainer", str(saved))
        expect((work / "global_glossary_tracker.json").exists(), "应重建 tracker")

        # 已存在且一致 → 不报错、不改动
        again = glossary_edit_service.add_term(work, None, term="云舟",
                                               translation="Memory Retainer")
        expect(again["ok"] and again["action"] == "unchanged", str(again))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_glossary_global_scope_is_shared_and_marked() -> None:
    """全局术语库要能在面板里新增，并在总表里标成「全局」（chapter = -1）。

    两层不同的东西：
      * 自动流程不许覆盖全局层 —— 这是翻译侧的不变量；
      * 面板里人工新增/修改全局条目 —— 这正是全局层被维护的方式。
    这条测试同时钉住"写得进去"和"标得出来"。
    """
    work = _make_term_work()
    # 单独的临时目录：不要放到 /tmp 根下，避免多次运行之间互相污染
    global_dir = Path(tempfile.mkdtemp(prefix="panel_global_"))
    global_path = global_dir / "glossary.json"
    try:
        res = glossary_edit_service.add_term(
            work, str(global_path), term="苍穹", translation="Skywood", scope="global")
        expect(res["ok"] and res["action"] == "added", str(res))
        expect(res["scope"] == "global", str(res))
        saved = json.loads(global_path.read_text(encoding="utf-8"))
        expect(saved["fixed_terms"]["苍穹"] == "Skywood", str(saved))
        # 全局条目不能落到章节文件里
        expect(not (work / "glossary_3.json").read_text(encoding="utf-8").count("苍穹"),
               "全局条目不应写进章节术语库")

        # 总表里标记为「全局」：chapter = -1
        tracker = lib.read_tracker(work)
        entry = (tracker.get("fixed_terms") or {}).get("苍穹") or {}
        expect(entry.get("chapter") == -1, f"应标记为全局: {entry}")
        expect(entry.get("value") == "Skywood", str(entry))

        # 再提交同一个值 → unchanged；提交不同值 → 需要 force
        again = glossary_edit_service.add_term(
            work, str(global_path), term="苍穹", translation="Skywood", scope="global")
        expect(again["ok"] and again["action"] == "unchanged", str(again))
        conflict = glossary_edit_service.add_term(
            work, str(global_path), term="苍穹", translation="Sky Realm", scope="global")
        expect(conflict["ok"] is False and conflict.get("conflict"), str(conflict))
        forced = glossary_edit_service.add_term(
            work, str(global_path), term="苍穹", translation="Sky Realm",
            scope="global", force=True)
        expect(forced["ok"] and forced["action"] == "overridden", str(forced))
        expect(json.loads(global_path.read_text(encoding="utf-8"))
               ["fixed_terms"]["苍穹"] == "Sky Realm")

        # 未知范围仍然拒绝
        bad = glossary_edit_service.add_term(
            work, str(global_path), term="甲乙", translation="AB", scope="galaxy")
        expect(bad["ok"] is False and "作用范围" in bad["error"], str(bad))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(global_dir, ignore_errors=True)


def test_glossary_four_categories_and_context_rules() -> None:
    """四类术语都要能写；固定术语填语境必须明确报错，不能静默丢掉。"""
    work = _make_term_work()
    gdir = Path(tempfile.mkdtemp(prefix="panel_global_cat_"))
    g = gdir / "glossary.json"
    try:
        for cat, term, value, ctx in (
            ("fixed_terms", "主城", "Major Hub", ""),
            ("contextual_terms", "系统", "The System", "界面提示"),
            ("aesthetic_sentences", "风雨欲来", "Calm before the storm", ""),
            ("cultural_nuances", "万", "非精确计数时表示极多", ""),
        ):
            res = glossary_edit_service.add_term(
                work, str(g), term=term, translation=value, category=cat,
                context=ctx, scope="global")
            expect(res["ok"], f"{cat} 应能写入: {res}")

        saved = json.loads(g.read_text(encoding="utf-8"))
        expect(saved["fixed_terms"]["主城"] == "Major Hub", str(saved))
        expect(saved["contextual_terms"]["系统"]["界面提示"] == "The System", str(saved))
        expect(saved["aesthetic_sentences"]["风雨欲来"] == "Calm before the storm", str(saved))
        expect(saved["cultural_nuances"]["万"].startswith("非精确"), str(saved))

        # 固定术语不接受语境：以前是静默丢弃，现在是明确拒绝
        bad = glossary_edit_service.add_term(
            work, str(g), term="甲乙", translation="AB",
            category="fixed_terms", context="某个语境", scope="global")
        expect(bad["ok"] is False and "语境" in bad["error"], str(bad))

        # 语境术语必须给语境
        bad2 = glossary_edit_service.add_term(
            work, str(g), term="丙丁", translation="CD",
            category="contextual_terms", scope="global")
        expect(bad2["ok"] is False and "语境" in bad2["error"], str(bad2))

        # 说明类允许中文值；术语类必须纯英文
        bad3 = glossary_edit_service.add_term(
            work, str(g), term="戊己", translation="中文译法",
            category="fixed_terms", scope="global")
        expect(bad3["ok"] is False and "纯英文" in bad3["error"], str(bad3))

        # 全局视图的数据源：类别与计数
        view = glossary_edit_service.read_global(str(g))
        expect(view["ok"] and view["total"] == 4, str(view))
        expect(view["counts"] == {"fixed_terms": 1, "contextual_terms": 1,
                                  "aesthetic_sentences": 1, "cultural_nuances": 1},
               str(view["counts"]))
        expect([c["id"] for c in view["categories"]] == list(glossary_edit_service.CATEGORIES),
               str(view["categories"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(gdir, ignore_errors=True)


def test_glossary_work_view_excludes_global_terms() -> None:
    """全局条目要有自己的视图：作品术语库只列本作品的词条。

    novelkit 的 tracker 会把全局条目记为 chapter = -1（这是对的，翻译时要用），
    但面板的作品视图要把它们分离出去，只用一行提示指向「全局术语库」。
    """
    work = _make_term_work()
    gdir = Path(tempfile.mkdtemp(prefix="panel_global_sep_"))
    g = gdir / "glossary.json"
    try:
        glossary_edit_service.add_term(work, str(g), term="苍穹", translation="Skywood",
                                       scope="global")
        # 用夹具里没有的词（主城已在第 0 章确立，会被先到先得拦住）
        glossary_edit_service.add_term(work, str(g), term="边城", translation="Border Town",
                                       category="fixed_terms", scope="chapter", chapter=3)

        tracker = lib.read_tracker(work)
        fixed = tracker.get("fixed_terms") or {}
        expect(fixed.get("苍穹", {}).get("chapter") == -1, f"全局条目应标 -1: {fixed}")
        expect(fixed.get("边城", {}).get("chapter") == 3, f"本作品条目应记来源章: {fixed}")

        # 作品视图的表格数据源：过滤掉 chapter == -1
        own = [k for k, v in fixed.items() if v.get("chapter") != -1]
        global_only = [k for k, v in fixed.items() if v.get("chapter") == -1]
        expect("边城" in own and "苍穹" not in own, str(own))
        expect(global_only == ["苍穹"], str(global_only))

        # 全局视图只列全局层，且标为全局
        view = glossary_edit_service.read_global(str(g))
        expect([e["term"] for e in view["entries"]] == ["苍穹"], str(view["entries"]))
        expect(view["entries"][0]["chapter"] == -1 and view["entries"][0]["scope"] == "global",
               str(view["entries"][0]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(gdir, ignore_errors=True)


def test_glossary_conflict_needs_force_and_rewrites_origin() -> None:
    work = _make_term_work()
    try:
        res = glossary_edit_service.add_term(work, None, term="主城", translation="Main City")
        expect(res["ok"] is False and res.get("conflict"), str(res))
        expect("第 0 章" in res["error"], str(res))
        # 未加 force 时第 0 章不能被改动
        expect(json.loads((work / "glossary_0.json").read_text(encoding="utf-8"))
               ["fixed_terms"]["主城"] == "Major Hub", "被拒绝时不应改动文件")

        forced = glossary_edit_service.add_term(work, None, term="主城",
                                                translation="Main City", force=True)
        expect(forced["ok"] and forced["action"] == "overridden", str(forced))
        expect(forced["chapter"] == 0, f"应回写来源章，实际 {forced['chapter']}")
        expect(json.loads((work / "glossary_0.json").read_text(encoding="utf-8"))
               ["fixed_terms"]["主城"] == "Main City", "来源章应被改写")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_glossary_validation_and_delete() -> None:
    work = _make_term_work()
    try:
        expect(glossary_edit_service.add_term(work, None, term="", translation="X")["ok"] is False)
        expect(glossary_edit_service.add_term(work, None, term="中文", translation="")["ok"] is False)
        expect(glossary_edit_service.add_term(work, None, term="English", translation="X")["ok"] is False,
               "词条必须含中文")
        expect(glossary_edit_service.add_term(work, None, term="中文", translation="含中文")["ok"] is False,
               "译法必须纯英文")
        expect(glossary_edit_service.add_term(work, None, term="中文", translation="En",
                                              category="contextual_terms")["ok"] is False,
               "语境术语必须给语境")

        # 语境术语
        ctx = glossary_edit_service.add_term(work, None, term="级", translation="Tier",
                                             category="contextual_terms", context="阶位")
        expect(ctx["ok"], str(ctx))
        got = json.loads((work / "glossary_3.json").read_text(encoding="utf-8"))
        expect(got["contextual_terms"]["级"]["阶位"] == "Tier", str(got))

        # 删除
        deleted = glossary_edit_service.delete_term(work, None, term="石昊")
        expect(deleted["ok"], str(deleted))
        expect("石昊" not in json.loads((work / "glossary_3.json").read_text(encoding="utf-8"))
               ["fixed_terms"], "删除应生效")
        expect(glossary_edit_service.delete_term(work, None, term="不存在")["ok"] is False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_glossary_lookup() -> None:
    work = _make_term_work()
    try:
        res = glossary_edit_service.search(work, None, "主城")
        expect(res["found"] and res["chapter"] == 0 and res["value"] == "Major Hub", str(res))
        expect(glossary_edit_service.search(work, None, "没有这个词")["found"] is False)
        expect(glossary_edit_service.search(work, None, "")["found"] is False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ==========================================================================
# Refine：批注合并与回填
# ==========================================================================

def test_refine_parse_rows_and_segment_plan() -> None:
    import refine

    expect(refine.parse_rows("3,7-9") == [3, 7, 8, 9])
    expect(refine.parse_rows("9-7") == [7, 8, 9])
    expect(refine.parse_rows("") == [])

    rows = [
        {"zh_parts": ["甲"], "en_parts": ["A"], "gap": None},
        {"zh_parts": [], "en_parts": ["B"], "gap": "zh"},
        {"zh_parts": ["乙"], "en_parts": ["C"], "gap": None},
    ]
    plan = refine.segment_plan(rows, [1, 2, 3])
    expect([p["row"] for p in plan] == [1, 3], f"没有原文的行应跳过: {plan}")
    expect(plan[0]["zh"] == "甲" and plan[0]["en"] == "A", str(plan[0]))


def test_refine_apply_revisions_keeps_other_rows() -> None:
    import refine

    rows = [
        {"zh_parts": ["甲"], "en_parts": ["A"], "gap": None},
        {"zh_parts": ["乙"], "en_parts": ["B"], "gap": None},
        {"zh_parts": ["丙"], "en_parts": ["C"], "gap": None},
    ]
    en = ["A", "B", "C"]
    plan = refine.segment_plan(rows, [2])
    new_en, notes, changed = refine.apply_revisions(
        en, rows, plan, {2: {"text": "B revised", "reason": "时态不统一"}})
    expect(new_en == ["A", "B revised", "C"], str(new_en))
    expect(changed == 1 and len(notes) == 1, str(notes))
    expect(notes[0]["reason"] == "时态不统一" and notes[0]["changed"] is True, str(notes[0]))
    expect(notes[0]["hash"], "应记录内容指纹以便判断批注是否过时")

    # 模型给的是原文 → changed=False，但仍保留理由
    _, notes2, changed2 = refine.apply_revisions(en, rows, plan, {2: {"text": "B", "reason": "无需修改"}})
    expect(changed2 == 0 and notes2[0]["changed"] is False, str(notes2))

    # 模型没返回某行 → 该行原样保留
    new_en3, _, _ = refine.apply_revisions(en, rows, plan, {})
    expect(new_en3 == en, str(new_en3))


def test_refine_splits_multiline_revision_into_paragraphs() -> None:
    """模型返回多段文本时必须拆成真正的多段。

    文件格式是"一段一行、段间空行"。以前把带 \\n\\n 的字符串当成一段写进去，
    段落数会悄悄变化、连带打乱对齐，批注就再也对不上段落了（用户实际踩到过）。
    """
    import refine

    rows = [
        {"zh_parts": ["甲"], "en_parts": ["A"], "gap": None},
        {"zh_parts": ["乙"], "en_parts": ["B"], "gap": None},
    ]
    en = ["A", "B"]
    plan = refine.segment_plan(rows, [2])
    new_en, notes, _ = refine.apply_revisions(
        en, rows, plan, {2: {"text": "--\n\nNote: rewritten", "reason": "拆成两段"}})
    expect(new_en == ["A", "--", "Note: rewritten"], f"应拆成两段: {new_en}")
    expect(notes[0]["after"] == "--\n\nNote: rewritten", str(notes[0]))

    # 段内单个换行也算分段（该格式下一行就是一段）
    new_en2, _, _ = refine.apply_revisions(
        en, rows, plan, {2: {"text": "line1\nline2", "reason": "r"}})
    expect(new_en2 == ["A", "line1", "line2"], str(new_en2))


def test_note_matches_across_reparagraphing() -> None:
    """批注匹配要对"一次修订覆盖多段"容错，否则图标会消失。"""
    import re as _re

    def flatten(value):
        return _re.sub(r"\s+", " ", str(value or "")).strip()

    def matched(after, current):
        a, c = flatten(after), flatten(current)
        if not a or not c:
            return False
        return a == c or (len(c) >= 12 and (a.startswith(c) or a.endswith(c)))

    # 完全一致
    expect(matched("Note: hello world", "Note: hello world"))
    # 修订跨两段，重新对齐后当前行只剩尾段（真实踩到的场景）
    expect(matched("--\n\nNote: This story incorporates elements",
                   "Note: This story incorporates elements"), "尾段应能匹配")
    # 短片段不参与首尾匹配："--" 这种太容易误伤别的行
    expect(not matched("--\n\nNote: This story incorporates elements", "--"),
           "过短的片段不应匹配")
    # 内容被改过 → 不应匹配（避免显示过时理由）
    expect(not matched("Note: original wording here", "Note: completely different text"),
           "文本不同不应匹配")


def test_refine_merge_notes_replaces_same_row() -> None:
    import refine

    existing = {"chapter": 1, "updated": "old", "notes": [
        {"row": 2, "reason": "旧理由", "after": "B", "changed": False, "hash": "x"},
        {"row": 5, "reason": "保留", "after": "E", "changed": True, "hash": "y"},
    ]}
    merged = refine.merge_notes(existing, [
        {"row": 2, "reason": "新理由", "after": "B2", "changed": True, "hash": "z"},
    ])
    by_row = {n["row"]: n for n in merged["notes"]}
    expect(len(merged["notes"]) == 2, str(merged))
    expect(by_row[2]["reason"] == "新理由", "同一行应被覆盖")
    expect(by_row[5]["reason"] == "保留", "其它行应保留")
    expect(merged["updated"] != "old", "应更新时间戳")


def test_refine_prompt_declares_revisions() -> None:
    from novelkit import prompt as nkprompt

    system, user = nkprompt.build_refine_messages(
        "SYS", [{"row": 4, "zh": "中文", "en": "English"}], {},
        extra_instruction="改动尽量小")
    expect("revisions" in system, "必须声明 revisions 输出格式")
    expect("[4]" in user and "原文: 中文" in user and "现译: English" in user, user[-400:])
    expect("<user_supplement>" in user and "改动尽量小" in user, "补充要求应注入")


# ==========================================================================
# 前端行为（源码级检查）
# ==========================================================================

def test_frontend_modules_are_self_consistent() -> None:
    """拆分后的 ES Module 必须自洽：import 的名字都得有人 export。

    这条不依赖浏览器，却能挡住"改了模块忘了同步 export"这类会让整页白屏的低级错误
    （浏览器遇到缺失的导出会直接拒绝加载整个模块图）。
    """
    modules = {p.name: p.read_text(encoding="utf-8") for p in JS_DIR.glob("*.js")}
    expect(modules, "static/js 下应有前端模块")

    exported: dict = {}
    for name, text in modules.items():
        block = re.search(r"export\s*\{([^}]*)\}", text)
        exported[name] = {item.strip() for item in (block.group(1).split(",") if block else [])
                          if item.strip()}

    for name, text in modules.items():
        for names, target in re.findall(
                r"import\s*\{([^}]*)\}\s*from\s*'\./([\w.-]+\.js)'", text):
            expect(target in modules, f"{name} 导入了不存在的模块 {target}")
            missing = sorted({item.strip() for item in names.split(",") if item.strip()}
                             - exported[target])
            expect(not missing, f"{name} 从 {target} 导入了未导出的名字: {missing}")

    html = (ROOT / "webpanel" / "static" / "index.html").read_text(encoding="utf-8")
    expect('type="module"' in html and "/static/js/main.js" in html,
           "index.html 应以 ES Module 方式加载 /static/js/main.js")
    expect(not (ROOT / "webpanel" / "static" / "app.js").exists(),
           "拆分后不应再保留单文件 app.js")


def test_static_js_served_with_javascript_mime() -> None:
    """ES Module 有严格 MIME 检查：.js 必须以 JS MIME 返回，否则整页加载失败。"""
    httpd, base = _start_panel()
    try:
        status, headers, body = _request_raw(f"{base}/static/js/main.js")
        ctype = headers.get("Content-Type", "").split(";")[0].strip()
        expect(status == 200, f"main.js 应可访问，实际 {status}")
        expect(ctype in ("text/javascript", "application/javascript"),
               f"模块脚本的 MIME 必须是 JS，实际 {ctype!r}")
        expect(b"import" in body, "返回的应是 main.js 源码")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_reading_position_memory_and_intro_default() -> None:
    """阅读位置要按作品记住，且首次进入优先落在简介。

    同样用源码级检查：这套逻辑散在三处（路由决定落点、pick 记录、renderReader 回退），
    少接一处就会退化成"每次进来都从第 1 章开始"或"沿用上一部作品的章号"。
    """
    raw = app_source()
    app = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    app = re.sub(r"//[^\n]*", "", app)

    expect("function rememberChapter(" in app, "应当有写入阅读位置的函数")
    expect("function lastChapterOf(" in app, "应当有读取阅读位置的函数")
    expect("function defaultChapterNum(" in app, "应当有落点决策函数")
    expect("localStorage.setItem(lastChapterKey(" in app, "阅读位置应存进浏览器存储")

    # 落点优先级：记得的章号（且仍存在）> 简介 > 第一章
    decide = app[app.index("function defaultChapterNum("):]
    decide = decide[:decide.index("\n}\n") + 3]
    expect("lastChapterOf(" in decide and "some(" in decide, "要先尝试记住的章号")
    expect("is_intro" in decide, "没有记忆时要优先简介")
    expect(decide.index("lastChapterOf(") < decide.index("is_intro"),
           "记忆应优先于简介")

    # 切章要记录
    pick = app[app.index("const pick = (num) => {"):]
    pick = pick[:pick.index("};") + 2]
    expect("rememberChapter(" in pick, "切换章节时要记录阅读位置")

    # 路由不带章号时必须清空沿用值，否则会拿上一部作品的章号去渲染
    route = app[app.index("if (view === 'reader') {"):]
    route = route[:route.index("await renderReader(summary);")]
    expect("state.reader.num = null" in route,
           "不带章号时要清掉上一部作品的章号，交给记忆/简介决定")


def test_no_literal_null_rendered() -> None:
    """回归：replaceChildren 收到 null 会把它渲染成文本 "null"。

    术语库为空时页面上出现过一个光秃秃的 null —— 原因是分支变量
    （globalNote / glossaryCard）可能是 null，直接传给了 replaceChildren。
    这类问题不值得靠人眼看，钉住"传进去的参数必须过滤或兜底"。
    """
    raw = app_source()
    app = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    app = re.sub(r"//[^\n]*", "", app)

    # 术语库视图：globalNote 为 null 时必须先过滤
    for match in re.finditer(r"replaceChildren\(([^;]*?)\);", app, re.S):
        args = match.group(1)
        line = app[:match.start()].count("\n") + 1
        if ".filter(Boolean)" in args:
            continue
        if re.fullmatch(r"[\s\w,\.\(\)\[\]'\"]+", args):
            # 纯变量参数：不允许出现可能为 null 的裸变量
            for risky in ("globalNote", "glossaryCard", "resultBox"):
                expect(risky not in args,
                       f"第 {line} 行把 {risky} 直接传给了 replaceChildren（可能渲染出 null）")

    # 术语库分支必须有过滤，正文的术语槽必须有兜底
    expect("...[controls, tabs, body, globalNote].filter(Boolean)" in app,
           "术语库视图应在 replaceChildren 前过滤掉 null")
    expect("glossaryCard || el('span')" in app,
           "正文术语槽应对空卡片兜底")
    expect("buildGlossaryCard(data) || el('span')" in app,
           "原地刷新路径同样要兜底")


def test_work_name_decoded_from_hash() -> None:
    """中文作品名必须能从 hash 里还原。

    location.hash 对非 ASCII 是已编码的（#/work/%E8%B5%9B…），
    不解码就匹配不到 state.works —— 中文名的作品会出现"作品不存在"或打不开。
    """
    raw = app_source()
    app = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    app = re.sub(r"//[^\n]*", "", app)

    body = app[app.index("function parseHash("):]
    body = body[:body.index("async function route(")]
    expect("decodeURIComponent" in body,
           "parseHash 必须解码 hash 里的作品名（中文名否则打不开）")
    expect("let work = parts[1]" in body or "work = decodeURIComponent" in body,
           "作品名应取自 hash 解码后的值")


def test_reader_search_and_delete_wiring() -> None:
    """阅读页的搜索跳转与「删除本章」都要真的接线（源码级）。"""
    raw = app_source()
    app = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    app = re.sub(r"//[^\n]*", "", app)

    expect("function gotoMatch(" in app, "应有搜索跳转函数")
    expect("mark-current" in app, "当前命中要有独立样式，便于定位")
    expect("state.reader.matchIndex" in app, "要记住当前定位到第几个命中")

    toolbar = app[app.index("const toolbar = el('div', { class: 'reader-toolbar' }"):]
    toolbar = toolbar[:toolbar.index("if (!state.snippetsLoadedFor")]
    expect("gotoMatch(1)" in toolbar and "gotoMatch(-1)" in toolbar,
           "工具栏要有上一个/下一个命中")
    expect("key === 'Enter'" in toolbar, "输入框里回车要跳到下一个")
    expect("e.shiftKey ? -1 : 1" in toolbar, "Shift+回车应回上一个")
    expect("删除本章" in toolbar, "阅读页要提供删除本章按钮")
    expect("function renameWork(" in app and "function deleteWork(" in app,
           "编辑页要提供改名与删除整本书")
    expect("lastChapterKey(oldName)" in app, "改名后阅读位置记忆要一起搬过去")

    # 提示卡是 fixed 定位，锚点一滚就错位，所以任何滚动都要收起（含固定住的）
    expect("addEventListener('scroll', hideTip" in app,
           "滚动时必须收起悬浮提示卡（否则它会停在原处盖住正文）")
    expect("scroll', () => { if (!tipPinned) hideTip(); }" not in app,
           "滚动收起不应再区分是否固定住")
    expect("function deleteCurrentChapter(" in app, "删除按钮要有对应实现")
    expect("method: 'DELETE'" in app, "删除要走 DELETE 接口")
    # 任务日志不能出现两份：卡片里那份已去掉，只留独立槽位
    actions = app[app.index("function buildChapterActions("):]
    actions = actions[:actions.index("async function ")]
    expect("makeJobLog" not in actions,
           "本章操作卡片里不应再放一份任务日志（会和 #job-slot 重复）")
    expect("id: 'job-slot'" in app, "任务日志要有唯一槽位")


def test_job_completion_does_not_rebuild_page() -> None:
    """任务结束时必须只更新本章内容，不能整页重绘。

    这条是回归防线：曾经因为改了一处**已经被重构过**的代码形状，
    补丁静默失效，任务完成仍然走 loadBootstrap()+route()，
    结果阅读区闪烁、滚动位置被清零。函数本身测过是好的，
    但"接线"没测到——所以这里直接检查源码。
    """
    raw = app_source()
    # 先去掉注释：注释里提到某个 API 不代表真的调用了它
    app = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    app = re.sub(r"//[^\n]*", "", app)

    handler = app[app.index("function makeJobLog("):app.index("function mountJob(")]
    expect("refreshChapterInPlace" in handler, "任务结束应当原地刷新内容")
    expect("loadBootstrap(); route()" not in handler,
           "任务结束不应整页重绘（会导致闪烁与滚动归零）")

    mount = app[app.index("function mountJob("):app.index("function mountJob(") + 600]
    expect("scrollIntoView" not in mount, "启动任务不应把用户的阅读位置拽走")

    # 原地刷新必须是"按行最小化"，不能整块 replaceChildren 正文
    refresh = app[app.index("async function refreshChapterInPlace"):
                  app.index("async function refreshChapterInPlace") + 4000]
    expect("patchReaderBody" in refresh, "原地刷新应走按行比对")
    expect("rowFingerprint" in app, "应当有行内容指纹")

    # 首次翻译完成后，这些"看起来还停在翻译前"的地方必须一起更新：
    # 下拉仍显示"未译"、按钮仍是"翻译本章"，都是用户实际报过的问题。
    expect("refreshChapterList" in refresh, "刷新时应重建章节下拉（否则一直显示未译）")
    expect("buildReaderHeader" in refresh, "刷新时应重建阅读区表头（段数/词数会变）")
    expect("actions-slot" in refresh, "刷新时应重建本章操作（翻译本章→重新翻译本章）")
    expect("loadBootstrap" in handler, "任务结束应刷新侧栏计数")
    # 任务日志在被重建的卡片之外，不能被顺手清掉
    expect("job-slot" in app, "任务日志应有独立槽位，避免被重建清掉")


# ==========================================================================
# 外部工具跳转
# ==========================================================================

def test_tool_url_validation() -> None:
    for bad in ("ht!tp://bad", "http://1.2.3.4:99999", "http://", "javascript:alert(1)"):
        url, error = tools_service.validate_url(bad)
        expect(error, f"{bad!r} 应被拒绝")
        expect(url == "", f"{bad!r} 不应给出规范化地址")
    for good, want in (("127.0.0.1:18423", "http://127.0.0.1:18423"),
                       ("http://127.0.0.1:18423/", "http://127.0.0.1:18423/"),
                       ("https://example.com/ui", "https://example.com/ui")):
        url, error = tools_service.validate_url(good)
        expect(not error and url == want, f"{good!r} -> {url!r} {error!r}")
    expect(tools_service.validate_url("") == ("", ""))


def test_tool_probe_is_deterministic() -> None:
    """可达性探测要同时答对"开着"和"关着"，且结果与机器上跑着什么无关。

    注意：不能"绑一个端口→关掉→断言不可达"。临时端口会立刻被系统回收，
    同机器上别的进程（面板自己、下载器）发起出站连接时可能正好分到它，
    断言就会偶发失败。改成**探测期间一直占着**这两个端口，消除竞态。
    """
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    open_port = listener.getsockname()[1]

    # 只 bind 不 listen：端口归我们占着，连接会被内核拒绝（RST），
    # 既不会被别的进程抢走，也确定不可达。
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    closed_port = holder.getsockname()[1]

    try:
        expect(tools_service.probe(f"http://127.0.0.1:{open_port}/") is True, "开着端口应判为可达")
        expect(tools_service.probe(f"127.0.0.1:{open_port}") is True, "缺 schema 也应能探测")
        expect(tools_service.probe(f"http://127.0.0.1:{closed_port}/") is False,
               "占着但未 listen 的端口应判为不可达")
        expect(tools_service.probe("http://127.0.0.1:1/") is False, "1 端口不应可达")
        expect(tools_service.probe("not a url") is False, "非法地址不应抛异常")
        expect(tools_service.probe("") is False, "空地址不应抛异常")
    finally:
        listener.close()
        holder.close()


def test_tool_registry_default_and_override() -> None:
    original = tools_service.TOOLS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="panel_tools_"))
    tools_service.TOOLS_PATH = tmp / "tools.json"
    try:
        tools = tools_service.list_tools()
        expect(len(tools) == 1 and tools[0]["id"] == "tomato", str(tools))
        # 侧栏显示的是中性名称；id 保持 tomato 不变，老 tools.json 里的覆盖配置不失效
        expect(tools[0]["label"] == "作品导出工具", str(tools[0]))
        expect("下载器" not in tools[0]["label"], "界面上不点名具体第三方项目")
        expect(tools[0]["url"] == tools_service.DEFAULTS[0]["url"], str(tools[0]))
        expect(tools[0]["url"] == "http://localhost:18423/", str(tools[0]))
        expect("hint" not in tools[0], "界面不使用 hint，接口也不必下发")
        expect(isinstance(tools[0]["reachable"], bool), "可达性必须是布尔值")

        saved = tools_service.set_url("tomato", "127.0.0.1:19999")
        expect(saved["ok"], str(saved))
        expect(saved["tools"][0]["url"] == "http://127.0.0.1:19999", str(saved["tools"]))
        expect(saved["tools"][0]["customised"] is True)

        expect(tools_service.set_url("tomato", "")["tools"][0]["url"]
               == tools_service.DEFAULTS[0]["url"], "空值应恢复默认")
        expect(tools_service.set_url("nope", "http://x")["ok"] is False, "未知工具应被拒绝")
        expect(tools_service.set_url("tomato", "ht!tp://bad")["ok"] is False, "非法地址应被拒绝")
    finally:
        tools_service.TOOLS_PATH = original
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# HTTP 路由（同一个路径同时注册 GET/POST 时最容易出错）
# ==========================================================================

def _start_panel():
    import threading
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(ROOT / "webpanel"))
    import server as panel_server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel_server.PanelHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _request(url: str, method: str = "GET", payload=None):
    import json as _json
    import urllib.error
    import urllib.request

    data = _json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, _json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, _json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw}


def _request_raw(url: str, method: str = "GET", *, follow: bool = True):
    """原始请求：返回 (状态, 响应头, 字节体)，用于验证反代的 HTML/JS/跳转/Cookie。"""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method=method)
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_http_method_routing() -> None:
    """回归：/api/settings/credentials 同时有 GET 与 POST，
    以前 POST 会先撞上先注册的 GET 路由，直接 405（用户实际遇到的就是这个）。"""
    with temp_env():
        httpd, base = _start_panel()
        try:
            status, body = _request(f"{base}/api/settings/credentials")
            expect(status == 200 and body.get("ok"), f"GET 失败: {status} {body}")

            # POST 必须能到达处理函数：空 body 应当是 400（缺参数），而不是 405
            status, body = _request(f"{base}/api/settings/credentials", "POST", {})
            expect(status == 400, f"POST 应到达处理函数并返回 400，实际 {status} {body}")
            expect("方法不允许" not in str(body.get("error", "")), str(body))

            # POST 到只有 POST 的路径，用 GET 访问 → 405
            status, _ = _request(f"{base}/api/import/epub/scan", "GET")
            expect(status == 405, f"应为 405，实际 {status}")

            # 不存在的路径 → 404
            status, _ = _request(f"{base}/api/nope")
            expect(status == 404, f"应为 404，实际 {status}")

            # 其它真实接口应保持可用
            for path in ("/api/health", "/api/works", "/api/jobs", "/api/tools"):
                status, body = _request(f"{base}{path}")
                expect(status == 200 and body.get("ok"), f"{path} 失败: {status}")
        finally:
            httpd.shutdown()


def test_http_credentials_post_does_not_touch_real_env() -> None:
    """通过 HTTP 保存凭据时，必须只写临时 .env，绝不碰真实文件。"""
    with temp_env() as env_path:
        httpd, base = _start_panel()
        try:
            status, body = _request(f"{base}/api/settings/credentials", "POST",
                                    {"API_KEY": "sk-from-http"})
            expect(status == 200 and body.get("ok"), f"{status} {body}")
            expect(f"{creds.API_KEY}=sk-from-http" in env_path.read_text(encoding="utf-8"),
                   "凭据未写入（临时）.env")
        finally:
            httpd.shutdown()


def test_tool_proxy_url_rewriting() -> None:
    """反代改写是纯函数，先把规则钉死：只改注册表里声明的前缀，且覆盖各种引号写法。"""
    import server as panel_server

    segs = ["/api/", "/assets/", "/download/", "/download-zip/"]
    prefix = "/tools/tomato"

    html = '<link href="/assets/app.css"><script src="/assets/app.js"></script>'
    out = panel_server.rewrite_tool_body(html, prefix, segs)
    expect('/tools/tomato/assets/app.css' in out and 'href="/assets/' not in out, out)

    js = ("await j(`/api/jobs/${id}`, {method:'POST'});"
          "fetch('/api/status');const z=\"/download/a.txt\";")
    out = panel_server.rewrite_tool_body(js, prefix, segs)
    expect("`/tools/tomato/api/jobs/" in out, out)
    expect("'/tools/tomato/api/status'" in out, out)
    expect('"/tools/tomato/download/a.txt"' in out, out)

    # 未声明的前缀不动（避免误伤正文里的普通路径）
    out = panel_server.rewrite_tool_body('x="/other/thing"', prefix, segs)
    expect(out == 'x="/other/thing"', out)

    # Location：站内相对 + 工具自身绝对地址都要收进子路径，外部地址原样放行
    expect(panel_server.rewrite_tool_location("/login", "http://127.0.0.1:18423/",
                                              prefix) == "/tools/tomato/login")
    expect(panel_server.rewrite_tool_location("http://127.0.0.1:18423/login",
                                              "http://127.0.0.1:18423/", prefix) == "/tools/tomato/login")
    expect(panel_server.rewrite_tool_location("https://github.com/x",
                                              "http://127.0.0.1:18423/", prefix) == "https://github.com/x")

    # Cookie 的 Path 收进子路径，避免下载器的会话 Cookie 跟着面板全局发
    expect(panel_server.rewrite_tool_cookie("s=1; Path=/; HttpOnly", prefix)
           == "s=1; Path=/tools/tomato/; HttpOnly")

    # 上游地址只允许打到配置里的那一台主机
    tool = {"url": "http://127.0.0.1:18423/"}
    target, error = panel_server.proxy_target(tool, "api/status", "a=1")
    expect(not error and target == "http://127.0.0.1:18423/api/status?a=1", (target, error))
    _, error = panel_server.proxy_target({"url": "not a url"}, "api", "")
    expect(error, "非法上游地址应被拒绝")


def test_tool_proxy_end_to_end() -> None:
    """真起一个"上游 WebUI"：面板 /tools/tomato/ 必须能转发并改写页面/接口/跳转/Cookie。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import server as panel_server

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音
            pass

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or []):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, b'<link href="/assets/app.css"><h1>Tomato DL</h1>',
                           "text/html; charset=utf-8")
            elif path == "/assets/app.js":
                self._send(200, b"fetch('/api/status');", "application/javascript",
                           [("Set-Cookie", "sess=abc; Path=/; HttpOnly")])
            elif path == "/api/status":
                self._send(200, b'{"ok":true,"self":"/api/status"}', "application/json")
            elif path == "/goto":
                self._send(302, b"", "text/plain", [("Location", "/login")])
            else:
                self._send(404, b"nope", "text/plain")

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    up_port = upstream.server_address[1]

    original = tools_service.TOOLS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="panel_proxy_"))
    tools_service.TOOLS_PATH = tmp / "tools.json"
    tools_service.TOOLS_PATH.write_text(
        json.dumps({"tomato": f"http://127.0.0.1:{up_port}/"}), encoding="utf-8")

    httpd, base = _start_panel()
    try:
        # 没有结尾斜杠 → 先跳转，保证相对路径解析正确
        status, headers, _ = _request_raw(f"{base}/tools/tomato", follow=False)
        expect(status == 302 and headers.get("Location") == "/tools/tomato/",
               (status, headers.get("Location")))

        status, headers, raw = _request_raw(f"{base}/tools/tomato/")
        body = raw.decode()
        expect(status == 200 and "Tomato DL" in body, (status, body[:120]))
        expect('/tools/tomato/assets/app.css' in body, body)
        expect(panel_server.TOOL_PROXY_ENABLED is True, "测试环境应默认开启反代")

        status, headers, raw = _request_raw(f"{base}/tools/tomato/assets/app.js")
        expect(status == 200 and "'/tools/tomato/api/status'" in raw.decode(), raw[:120])
        cookie = headers.get("Set-Cookie", "")
        expect("Path=/tools/tomato/" in cookie, cookie)

        status, _, raw = _request_raw(f"{base}/tools/tomato/api/status")
        payload = json.loads(raw.decode())
        expect(status == 200 and payload["ok"] is True, payload)
        expect(payload["self"] == "/tools/tomato/api/status", payload)

        status, headers, _ = _request_raw(f"{base}/tools/tomato/goto", follow=False)
        expect(status == 302 and headers.get("Location") == "/tools/tomato/login",
               (status, headers.get("Location")))

        # 未注册的工具 id 不能借反代打到别处
        status, _, _ = _request_raw(f"{base}/tools/nope/")
        expect(status == 404, status)

        # --no-tool-proxy 时不再转发
        panel_server.TOOL_PROXY_ENABLED = False
        try:
            status, _, _ = _request_raw(f"{base}/tools/tomato/api/status")
            expect(status == 403, status)
        finally:
            panel_server.TOOL_PROXY_ENABLED = True
    finally:
        httpd.shutdown()
        upstream.shutdown()
        tools_service.TOOLS_PATH = original
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================

def main() -> int:
    tests = [
        ("凭据: all_set 作为翻译前置条件", test_credentials_all_set_gates_translation),
        ("凭据: 可选模型配置项", test_optional_model_keys_do_not_break_all_set),
        ("凭据: 保存不改动其它行", test_credentials_save_preserves_other_lines),
        ("凭据: 缺失键追加", test_credentials_save_appends_missing_key),
        ("凭据: 去掉误粘的前缀", test_credentials_save_strips_prefixed_key),
        ("凭据: 状态不回显密钥", test_credentials_status_never_leaks_values),
        ("凭据: dry-run 不落盘", test_credentials_dry_run_does_not_write),
        ("凭据: 无效输入拒绝且不落盘", test_credentials_save_rejects_garbage),
        ("凭据: 验证接口缺 Key / 成功", test_credentials_verify_reports_missing_key_and_ok),
        ("EPUB: 扫描列出条目", test_epub_scan_lists_items),
        ("EPUB: 条目分类（章节/目录/前置）", test_epub_scan_classifies_entries),
        ("EPUB: 简介不重复作为正文写入", test_epub_commit_intro_is_not_written_as_body),
        ("EPUB: 按选择导入 + 简介", test_epub_commit_selection_and_intro),
        ("EPUB: 可去掉简介/改起始章号", test_epub_commit_intro_optional_and_start_number),
        ("EPUB: 默认拒绝覆盖", test_epub_commit_refuses_overwrite_by_default),
        ("EPUB: 拒绝非法目录名", test_epub_commit_rejects_bad_work_name),
        ("对齐: 修复截图中的串行", test_alignment_fixes_the_reported_drift),
        ("对齐: 不丢不重", test_alignment_covers_everything_exactly_once),
        ("对齐: 极端输入不崩", test_alignment_handles_pathological_input),
        ("编辑: 写前备份 + 原子替换", test_editor_save_backs_up_and_is_atomic),
        ("编辑: 新建章节 / 未修改识别", test_editor_creates_new_chapter_and_reports_unchanged),
        ("编辑: 新建拒绝覆盖已有章节", test_editor_create_refuses_to_overwrite_existing),
        ("编辑: 删除本章并备份", test_editor_delete_chapter_backs_up),
        ("作品: 改名与删除（移入回收站）", test_work_rename_and_delete),
        ("编辑: 拒绝非法参数", test_editor_rejects_bad_side_and_negative_number),
        ("编辑: 库存与缺侧提示", test_editor_inventory_lists_missing_sides),
        ("任务: 生命周期与日志", test_job_lifecycle_and_log),
        ("任务: 失败与取消", test_job_failure_and_cancel),
        ("任务: SSE 增量推送", test_job_stream_sse_pushes_incremental_output),
        ("局部重译: 只改选中行", test_merge_translations_only_touches_selected_rows),
        ("局部重译: 合并行整体替换", test_merge_translations_handles_merged_row),
        ("局部重译: 拆分行合并替换", test_merge_translations_handles_split_row),
        ("局部重译: 缺输出时保持原样", test_merge_translations_skips_rows_without_model_output),
        ("局部重译: 标点规范化", test_merge_translations_normalises_punctuation),
        ("局部重译: 相邻行不吞补译段", test_merge_translations_consecutive_rows_keep_added_paragraph),
        ("局部重译: gap 行不吞相邻旧英文", test_merge_translations_gap_then_next_row_keeps_source),
        ("局部重译: 行号解析", test_retranslate_parse_rows),
        ("提示词: 共享且注入一致", test_prompt_module_is_shared_and_consistent),
        ("提示词: 局部重译保留上下文/术语", test_partial_prompt_keeps_context_and_glossary),
        ("提示词: translate.py 复用共享模块", test_main_uses_the_shared_prompt_module),
        ("术语: 译法拆分与清洗", test_split_renderings_handles_multiple_and_rejects_junk),
        ("术语: 暴露英文译法供高亮", test_chapter_terms_expose_english_renderings),
        ("术语: 只返回正文中出现的词", test_chapter_terms_only_returns_terms_present_in_text),
        ("补充要求: 增删改与落文件", test_snippets_roundtrip_and_limits),
        ("术语: 新增词条", test_glossary_add_new_term),
        ("术语: 全局作用范围与总表标记", test_glossary_global_scope_is_shared_and_marked),
        ("术语: 四类分类与语境规则", test_glossary_four_categories_and_context_rules),
        ("术语: 作品视图不含全局条目", test_glossary_work_view_excludes_global_terms),
        ("术语: 冲突需 force 且回写来源章", test_glossary_conflict_needs_force_and_rewrites_origin),
        ("术语: 校验与删除", test_glossary_validation_and_delete),
        ("术语: 查询", test_glossary_lookup),
        ("Refine: 行号与段落挑选", test_refine_parse_rows_and_segment_plan),
        ("Refine: 只改选中行且留理由", test_refine_apply_revisions_keeps_other_rows),
        ("Refine: 多段修订要拆成多段", test_refine_splits_multiline_revision_into_paragraphs),
        ("Refine: 跨段批注仍能匹配", test_note_matches_across_reparagraphing),
        ("Refine: 批注按行合并", test_refine_merge_notes_replaces_same_row),
        ("Refine: 提示词声明 revisions", test_refine_prompt_declares_revisions),
        ("前端: 模块自洽（import/export + 入口）", test_frontend_modules_are_self_consistent),
        ("前端: .js 以 JS MIME 返回", test_static_js_served_with_javascript_mime),
        ("前端: 阅读位置记忆 + 默认进简介", test_reading_position_memory_and_intro_default),
        ("前端: 不渲染字面量 null", test_no_literal_null_rendered),
        ("前端: hash 里的中文作品名要解码", test_work_name_decoded_from_hash),
        ("前端: 搜索跳转 + 删除本章接线", test_reader_search_and_delete_wiring),
        ("前端: 任务结束不整页重绘", test_job_completion_does_not_rebuild_page),
        ("工具: 地址校验", test_tool_url_validation),
        ("工具: 可达性探测确定性", test_tool_probe_is_deterministic),
        ("工具: 注册表与覆盖", test_tool_registry_default_and_override),
        ("工具: 反代前缀改写", test_tool_proxy_url_rewriting),
        ("工具: 反代端到端转发", test_tool_proxy_end_to_end),
        ("HTTP: 方法路由（GET/POST 同路径）", test_http_method_routing),
        ("HTTP: 凭据 POST 只写临时 .env", test_http_credentials_post_does_not_touch_real_env),
    ]
    for name, func in tests:
        check(name, func)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 66)
    print(f"webpanel 测试: {passed}/{len(RESULTS)} 通过")
    print("=" * 66)
    for name, ok, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print("        " + detail.replace("\n", "\n        "))
    print("=" * 66)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
