"""LLM 调用层：客户端构建、指数退避重试、健壮的 JSON 抽取。

旧版的两个真实故障（都能在 log/ 与 nohup.out 里找到）：
  * `[!] 解析失败: AI 返回内容无法解析 JSON：嗯，用户提交了一段中文小说...`
    —— 模型把思考过程当成正文返回，旧代码用贪婪正则 `\\{.*\\}` 去抠 JSON 失败后直接抛错，
       整批任务被 `break` 中断。
  * `Connection error.` —— 没有任何重试，一次网络抖动就丢一章。

本模块对应地提供：
  * ResponseParseError（可重试）：解析失败时自动重试而不是直接判死；
  * 平衡括号扫描 + 尾逗号修复 + 纯文本兜底(salvage)；
  * 统一的重试策略（连接错误 / 超时 / 429 / 5xx / 解析失败）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any, Callable, Dict, Iterable, Optional

from . import config

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_RETRYABLE_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "ConflictError",
}

_RETRYABLE_KEYWORDS = (
    "connection",
    "timed out",
    "timeout",
    "rate limit",
    "rate_limit",
    "temporarily",
    "overloaded",
    "try again",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "reset by peer",
    "eof occurred",
    "429",
    "500",
    "502",
    "503",
    "504",
)


class NovelKitError(RuntimeError):
    """本工具链所有可预期错误的基类。"""


class ResponseParseError(NovelKitError):
    """模型返回内容无法解析为约定的 JSON —— 可重试。"""

    def __init__(self, message: str, raw: str = "", reasoning: str = ""):
        super().__init__(message)
        self.raw = raw
        self.reasoning = reasoning


class DependencyError(NovelKitError):
    """缺少第三方依赖。"""


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

def _import_openai():
    try:
        import openai  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise DependencyError(
            "缺少 openai 依赖，请先安装：\n    pip install -r requirements.txt"
        ) from exc
    return openai


def create_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    timeout: float = 300.0,
    max_retries: int = 0,
):
    """同步客户端。

    注意 timeout 必须显式设置：旧版未设置超时，一次挂起的请求会让整个
    nohup 后台任务永久卡死；max_retries 交给本模块自己的退避逻辑，
    因此这里固定为 0，避免两层重试互相叠加。
    """
    openai = _import_openai()
    return openai.OpenAI(
        api_key=api_key or config.get_api_key(),
        base_url=base_url or config.DEFAULT_BASE_URL,
        timeout=timeout,
        max_retries=max_retries,
    )


def create_async_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    timeout: float = 300.0,
    max_retries: int = 0,
):
    openai = _import_openai()
    return openai.AsyncOpenAI(
        api_key=api_key or config.get_api_key(),
        base_url=base_url or config.DEFAULT_BASE_URL,
        timeout=timeout,
        max_retries=max_retries,
    )


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------

def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试。"""
    if isinstance(exc, (ResponseParseError, asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True

    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status is not None:
        try:
            return int(status) in RETRYABLE_STATUS
        except (TypeError, ValueError):
            pass

    name = type(exc).__name__
    if name in _RETRYABLE_NAMES:
        return True
    # 认证/参数类错误重试没有意义
    if name in {"AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError"}:
        return False

    message = str(exc).lower()
    return any(keyword in message for keyword in _RETRYABLE_KEYWORDS)


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """指数退避 + 抖动，避免多任务同时重试造成惊群。"""
    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return delay * (0.7 + random.random() * 0.6)


def call_chat(
    client,
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    **kwargs,
):
    """带退避重试的同步 chat.completions.create。"""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            return client.chat.completions.create(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - 需要按类型分流
            last_exc = exc
            if attempt > retries or not is_retryable(exc):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
    raise last_exc  # pragma: no cover - 逻辑上不可达


def ask_terms(
    client,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.2,
    extra_body: Optional[Dict[str, Any]] = None,
    retries: int = 0,
) -> str:
    """RAG 关键词提名用的一次性请求：返回模型回复原文。

    提示词与结果校验在 novelkit.rag（build_proposal_messages / parse_proposals），
    这里只负责发请求，保证各脚本用的是同一套参数形态。
    """
    params: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    if extra_body:
        params["extra_body"] = extra_body
    response = call_chat(client, retries=retries, **params)
    return response.choices[0].message.content or ""


async def acall_chat(
    client,
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    **kwargs,
):
    """带退避重试的异步 chat.completions.create。"""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            return await client.chat.completions.create(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt > retries or not is_retryable(exc):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover


def is_param_error(exc: BaseException) -> bool:
    """判断异常是否属于"这组请求参数模型不支持"。

    日志里同一套脚本跑过 qwen3-max / deepseek-r1 / deepseek-v3.2，
    它们对 response_format、enable_thinking、thinking_budget 的支持并不一致。
    命中这类错误时应当换一组精简参数重试，而不是直接失败。

    注意：必须先把鉴权/权限类错误排除掉。OpenAI 的错误体类型统一是
    "invalid_request_error"，如果拿它当特征，401 也会被误判成参数问题，
    于是白白把 6 组参数各试一遍。
    """
    status = getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None

    name = type(exc).__name__
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return False
    if status_int in {401, 403}:
        return False

    if name in {"BadRequestError", "UnprocessableEntityError", "InvalidRequestError"}:
        return True
    if status_int in {400, 404, 415, 422}:
        return True

    message = str(exc).lower()
    hints = (
        "response_format",
        "enable_thinking",
        "thinking_budget",
        "repetition_penalty",
        "unsupported",
        "not supported",
        "invalid parameter",
        "unrecognized",
        "unknown parameter",
        "does not support",
    )
    return any(hint in message for hint in hints)


def param_error_hint(exc: BaseException) -> set:
    """从报错信息里推断"到底是哪个参数不被支持"。

    有了这个提示，参数降级可以一步到位（直接挑掉罪魁祸首的参数组），
    而不用从"完整参数"逐个往下试。
    """
    message = str(exc).lower()
    offenders = set()
    if "response_format" in message or "json_object" in message:
        offenders.add("response_format")
    if any(k in message for k in ("enable_thinking", "thinking_budget", "extra_body", "repetition_penalty")):
        offenders.add("extra_body")
    if "presence_penalty" in message:
        offenders.add("presence_penalty")
    if "temperature" in message:
        offenders.add("temperature")
    if "top_p" in message:
        offenders.add("top_p")
    return offenders


def run_with_retry(
    func: Callable[[], Any],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> Any:
    """通用重试包装：把"网络抖动"和"模型没吐合法 JSON"放在同一个退避循环里。

    这样一次解析失败会重新请求，而不是像旧版那样直接终止整批任务。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            return func()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt > retries or not is_retryable(exc):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
    raise last_exc  # pragma: no cover


async def arun_with_retry(
    func: Callable[[], Any],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> Any:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt > retries or not is_retryable(exc):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover


# --------------------------------------------------------------------------
# JSON 抽取
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _balanced_objects(text: str) -> Iterable[str]:
    """扫描出所有"顶层"平衡的 {...} 片段（正确处理字符串与转义）。"""
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : index + 1]
                    start = -1


def _try_loads(candidate: str) -> Optional[Any]:
    if not candidate:
        return None
    variants = (
        candidate,
        candidate.strip(),
        _TRAILING_COMMA_RE.sub(r"\1", candidate),
        _TRAILING_COMMA_RE.sub(r"\1", candidate).strip(),
    )
    for variant in variants:
        try:
            return json.loads(variant, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def extract_json(text: str) -> Any:
    """尽最大努力从模型输出中抽出 JSON 对象。

    依次尝试：整体解析 → 去 ``` 围栏 → 平衡括号扫描 → 贪婪正则兜底。
    """
    if not text or not text.strip():
        raise ResponseParseError("模型返回内容为空")

    cleaned = text.strip()
    candidates = [cleaned]
    candidates.extend(match.group(1) for match in _FENCE_RE.finditer(cleaned))
    candidates.extend(_balanced_objects(cleaned))
    greedy = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if greedy:
        candidates.append(greedy.group(0))

    for candidate in candidates:
        parsed = _try_loads(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed

    raise ResponseParseError("模型返回内容无法解析为 JSON", raw=text)


# --------------------------------------------------------------------------
# 输出规范化
# --------------------------------------------------------------------------

_CONTENT_KEYS = (
    "translated_content",
    "translation",
    "translated_text",
    "translate_content",
    "content",
    "text",
    "译文",
    "英文正文",
)

_FIXED_KEYS = ("new_fixed_terms", "fixed_terms", "new_terms")
_CONTEXT_KEYS = ("new_contextual_terms", "contextual_terms")
_AESTHETIC_KEYS = ("new_aesthetic_sentences", "aesthetic_sentences")
_CULTURE_KEYS = ("new_cultural_nuances", "cultural_nuances")


def _pick(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _as_str_map(value: Any) -> Dict[str, str]:
    """把任意值收敛为 {str: str}，丢弃结构性错误的数据。"""
    result: Dict[str, str] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if isinstance(item, str) and item.strip():
            result[str(key)] = item.strip()
        elif isinstance(item, (list, tuple)):
            parts = [str(x).strip() for x in item if isinstance(x, str) and x.strip()]
            if parts:
                result[str(key)] = "; ".join(parts)
    return result


def _as_context_map(value: Any) -> Dict[str, Dict[str, str]]:
    """contextual_terms 允许两种形态：{term: {ctx: trans}} 或 {term: trans}。"""
    result: Dict[str, Dict[str, str]] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if isinstance(item, dict):
            inner = {
                str(ctx): trans.strip()
                for ctx, trans in item.items()
                if isinstance(trans, str) and trans.strip()
            }
            if inner:
                result[str(key)] = inner
        elif isinstance(item, str) and item.strip():
            result[str(key)] = {"default": item.strip()}
    return result


def coerce_translation_payload(data: Any) -> Dict[str, Any]:
    """把模型返回的 JSON 规范化成统一结构。

    这一步消除了旧版最脆弱的地方：只要模型把 new_contextual_terms 写成
    {term: "译文"}，旧代码的 `merged[...].update(v)` 就会抛
    'str' object does not support item assignment 并中断整批任务。
    """
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), {})
    if not isinstance(data, dict):
        raise ResponseParseError(f"模型返回的不是 JSON 对象，而是 {type(data).__name__}")

    content = _pick(data, _CONTENT_KEYS)
    if content is None:
        # 退一步：取最长的字符串字段，通常就是正文
        strings = [v for v in data.values() if isinstance(v, str)]
        content = max(strings, key=len) if strings else ""
    if not isinstance(content, str):
        content = str(content)

    return {
        "translated_content": content.strip(),
        "new_fixed_terms": _as_str_map(_pick(data, _FIXED_KEYS) or {}),
        "new_contextual_terms": _as_context_map(_pick(data, _CONTEXT_KEYS) or {}),
        "new_aesthetic_sentences": _as_str_map(_pick(data, _AESTHETIC_KEYS) or {}),
        "new_cultural_nuances": _as_str_map(_pick(data, _CULTURE_KEYS) or {}),
    }


def looks_like_english_prose(text: str) -> bool:
    """判断一段纯文本是否像"英文译文"（用于解析失败时兜底挽救）。"""
    from . import text as nktext

    if not text or len(text.strip()) < 20:
        return False
    if nktext.has_cjk(text):
        return False
    sample = text[:4000]
    ascii_letters = sum(1 for ch in sample if ch.isascii() and (ch.isalpha() or ch.isspace()))
    return ascii_letters / max(1, len(sample)) > 0.75


def salvage_plain_translation(text: str) -> Optional[Dict[str, Any]]:
    """模型没按 JSON 输出、但正文本身是像样的英文时，直接当成译文使用。

    比直接失败重跑一整章更划算，但调用方应当记录一条明显的警告。
    """
    if not looks_like_english_prose(text):
        return None
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    return coerce_translation_payload({"translated_content": stripped.strip()})


def extract_usage(response) -> Dict[str, int]:
    """抽取 token 用量，便于统计成本。"""
    usage = getattr(response, "usage", None)
    if not usage:
        return {}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {}
    for field in fields:
        value = getattr(usage, field, None)
        if isinstance(value, int):
            result[field] = value
    return result
