import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from time import perf_counter
from typing import List

import requests
from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDERS,
    get_llm_provider,
)
from app.utils import utils
from app.utils.request_logging import trace_sdk_call

_max_retries = 5
MIN_SCRIPT_PARAGRAPH_NUMBER = 1
MAX_SCRIPT_PARAGRAPH_NUMBER = 10
MAX_SCRIPT_PROMPT_LENGTH = 2000
MAX_SCRIPT_SYSTEM_PROMPT_LENGTH = 8000
# “测试模型链接”只发送一条极小请求。OpenAI SDK 默认 600 秒超时并自动重试
# 2 次，端点不可达（代理、防火墙或 DNS 故障）时，测试按钮会先长时间挂起，
# 最后只留下一句 "Request timed out."（见 issue #1642）。这里给连接测试一个
# 有界的短超时并禁用 SDK 重试，让失败快速、可诊断；正式的脚本生成链路保持
# SDK 默认行为不变，避免误伤慢速推理模型和本地 Ollama 冷启动。
LLM_CONNECTION_TEST_TIMEOUT_SECONDS = 30.0
LLM_CONNECTION_TEST_MAX_RETRIES = 0
# 这些片段来自 OpenAI SDK 的 APITimeoutError / APIConnectionError 以及
# httpx 的底层连接异常文案；命中任意一个即按“端点不可达”处理。
_CONNECTION_FAILURE_MARKERS = (
    "timed out",
    "timeout",
    "connection error",
    "connection refused",
    "connection reset",
    "connection aborted",
    "failed to connect",
)
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_UNOPENED_THINK_BLOCK_RE = re.compile(r"^.*</think\s*>", re.IGNORECASE | re.DOTALL)
_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]+@)", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)([^&#\s]+)",
    re.IGNORECASE,
)

# 约束 1-8 是下游流程依赖的输出契约（纯文本、无标题、段落数、语言），不要改动。
# 9-14 只约束写法：本地中小模型在默认提示词下容易写成百科式开头、长度一致的
# 长句，并以“in conclusion”之类的套话收尾；这几条针对的就是这些问题，对
# 托管模型同样适用，且不改变返回格式。
DEFAULT_SCRIPT_SYSTEM_PROMPT = """
# Role: Video Script Generator

## Goals:
Write the spoken narration for a short video on the given subject: prose that
holds attention from its first sentence to its last.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.

## Writing:
9. open on a hook. the first sentence must state something concrete and surprising about the subject — never a definition, a date range, or a sentence that begins by explaining what the subject is.
10. vary sentence length. follow a long sentence with a short one. a three-word sentence is allowed, and is often what makes a point land.
11. prefer a concrete detail to a summary: a number, a date, a place, a name, an object someone could photograph. one specific fact is worth more than three general claims.
12. stop on the last real thing you have to say. never write a closing summary, and never use "in conclusion", "in summary", "ultimately", "to this day", "stands as a testament", "underscoring", "paved the way" or any similar flourish.
13. no filler openings ("did you know", "imagine a world", "since the dawn of time"), and no rhetorical questions addressed to the audience.
14. this will be read aloud by a speech synthesiser. write words that are easy to say, and avoid parentheses, asides set off by dashes, lists, and abbreviations that have to be spelled out.
""".strip()

# Claude Code CLI 默认使用编码 agent 的系统提示词，其中大量约束与文案写作
# 无关，会让脚本和关键词生成偏离要求，因此调用时整体替换掉。
CLAUDE_CODE_SYSTEM_PROMPT = (
    "You are a concise copywriter. Follow the user's instructions and output "
    "format exactly, and output nothing else."
)
CLAUDE_CODE_DEFAULT_TIMEOUT = 300.0
# `--tools ""` 关闭全部内置工具，`--safe-mode` 关闭 CLAUDE.md、skills、hooks、
# plugins、MCP 等所有用户级定制，同时保持鉴权、模型选择和权限正常工作。
# 二者需要较新的 CLI；低版本会以 "unknown option" 退出，由调用处转成明确提示。
CLAUDE_CODE_MIN_CLI_VERSION = "2.1.260"
# 这些环境变量会让 CLI 改用 API Key 或第三方供应商（Bedrock、Vertex、Foundry、
# Mantle、Gateway 等），从而绕过订阅登录并产生额外计费。逐个列举容易漏项，
# 而且 CLI 后续还会新增供应商，因此按前缀整类剔除：
#   ANTHROPIC_*           API Key、Auth Token、Base URL、各家供应商端点和 Profile
#   CLAUDE_CODE_USE_*     供应商开关
#   CLAUDE_CODE_SKIP_*_AUTH  跳过供应商鉴权的开关
CLAUDE_CODE_CONFLICTING_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
CLAUDE_CODE_CONFLICTING_ENV_VARS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
)
# 这两类变量不能剔除：
#   CLAUDE_CODE_OAUTH_TOKEN 是容器内唯一的订阅鉴权方式（不匹配上面的前缀）；
#   *_CONFIG_DIR 只是指出凭证存放位置，剔除后反而会让已登录的订阅失效。
CLAUDE_CODE_PRESERVED_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_CONFIG_DIR",
    "CLAUDE_CONFIG_DIR",
)


def _is_conflicting_claude_code_env(name: str) -> bool:
    """判断某个环境变量是否会把 CLI 从订阅登录切换到别的鉴权方式。"""
    if name in CLAUDE_CODE_PRESERVED_ENV_VARS:
        return False
    if name in CLAUDE_CODE_CONFLICTING_ENV_VARS:
        return True
    if name.startswith(CLAUDE_CODE_CONFLICTING_ENV_PREFIXES):
        return True
    return name.startswith("CLAUDE_CODE_SKIP_") and name.endswith("_AUTH")


def coerce_claude_code_timeout(value, config_key: str = "claude_code_timeout"):
    """
    把配置里的超时值解析成正的有限秒数。

    TOML 既可能写成 `claude_code_timeout = 300`（int/float），也可能写成
    `"300"`（字符串），因此不能直接调用 `strip()`。nan / inf 会让
    `subprocess.run(timeout=...)` 永久阻塞，这里一并拒绝。
    """
    if value is None:
        return CLAUDE_CODE_DEFAULT_TIMEOUT

    if isinstance(value, bool):
        # bool 是 int 的子类，但 True 秒显然不是用户想要的超时配置。
        raise ValueError(f"{config_key} must be a number of seconds, got {value!r}")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return CLAUDE_CODE_DEFAULT_TIMEOUT
        try:
            seconds = float(text)
        except ValueError:
            raise ValueError(
                f"{config_key} must be a number of seconds, got {value!r}"
            ) from None
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        raise ValueError(f"{config_key} must be a number of seconds, got {value!r}")

    if not math.isfinite(seconds):
        raise ValueError(f"{config_key} must be a finite number, got {value!r}")
    if seconds <= 0:
        raise ValueError(f"{config_key} must be greater than 0, got {value!r}")
    return seconds


def _resolve_provider_field_value(raw_value, default_value):
    """
    只有「未配置」时才回退到 Registry 默认值。

    之前用 `raw or default_value`，会把 0 和 false 这类合法取值也当成未配置
    替换掉：`claude_code_timeout = 0` 被静默改成 300，而 `"0"` 却报错。默认值
    只在 None 或空白字符串时生效，配置校验才能对所有写法保持一致。
    """
    if raw_value is None:
        return default_value
    if isinstance(raw_value, str) and not raw_value.strip():
        return default_value
    return raw_value


def build_claude_code_env(base_env=None):
    """
    构造只依赖订阅登录的子进程环境。

    返回 (环境变量字典, 被剔除的变量名列表)。剔除的是会切换鉴权方式或供应商
    的变量，`CLAUDE_CODE_OAUTH_TOKEN` 必须保留：容器内没有 keychain，CLI 只能
    靠它完成订阅鉴权。
    """
    env = dict(os.environ if base_env is None else base_env)
    removed = sorted(name for name in env if _is_conflicting_claude_code_env(name))
    for name in removed:
        env.pop(name, None)
    return env, removed


def _normalize_text_response(content, llm_provider: str) -> str:
    # 不同 LLM SDK 在异常或被拦截场景下，可能返回 None、空字符串，
    # 甚至返回非字符串对象。这里统一做兜底校验，避免后续直接调用
    # `.replace()` 时抛出 `NoneType` 之类的属性错误。
    if content is None:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    if not isinstance(content, str):
        raise TypeError(
            f"[{llm_provider}] returned non-text content: {type(content).__name__}"
        )

    # MiniMax M3、DeepSeek R1 这类 reasoning 模型可能会把内部推理包在
    # `<think>...</think>` 中返回。视频脚本和关键词只需要最终可朗读文本，
    # 如果不在服务层统一清理，WebUI、字幕和配音都会把思考过程当正文处理。
    content = _THINK_BLOCK_RE.sub("", content)
    # DeepSeek R1、QwQ 等模型的对话模板会把起始 `<think>` 放进提示词，
    # vLLM/Ollama 等网关返回的正文只剩 `推理...</think>答案`。成对清理之后
    # 仍残留的闭合标签之前都是推理内容，也不能进入脚本。
    content = _UNOPENED_THINK_BLOCK_RE.sub("", content)
    content = _UNCLOSED_THINK_BLOCK_RE.sub("", content).strip()
    if not content:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    # 前面的 ``strip()`` 已经清理首尾空白。这里必须保留正文中的单换行和
    # 双换行：脚本生成依赖双换行区分段落，字幕处理也会按行读取用户文案。
    return content


def _sanitize_error_message(error: object) -> str:
    """
    清理返回给 WebUI/API 的错误信息，避免自定义 base_url 中的凭据泄露。

    一些 OpenAI-compatible SDK 会把请求 URL 原样拼进异常信息。如果用户为了
    代理网关配置了 `https://user:pass@example.com/v1`，直接返回 `str(e)`
    就会把密码暴露给页面、API 调用方或后续日志。这里仅处理错误文案，不改变
    实际请求地址，避免影响正常调用链路。
    """
    message = str(error)
    message = _URL_USERINFO_RE.sub(r"\1***:***@", message)
    message = _SENSITIVE_QUERY_RE.sub(r"\1***", message)
    return message


def _extract_chat_completion_text(response, llm_provider: str) -> str:
    # OpenAI 兼容接口在异常场景下，可能返回没有 choices、
    # 或者 choices/message/content 为空的响应对象。
    # 这里统一做结构校验，避免出现 `NoneType is not subscriptable`
    # 这类底层属性访问错误。
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{llm_provider}] returned empty choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError(f"[{llm_provider}] returned empty message")

    content = getattr(message, "content", None)
    return _normalize_text_response(content, llm_provider)


def _get_response_field(value, key: str):
    """兼容 dict 和 SDK 响应对象的字段读取。"""
    if isinstance(value, dict):
        return value.get(key)

    try:
        return value[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(value, key, None)


def _sdk_transport_options(request_timeout, max_retries) -> dict:
    """把可选的超时/重试约束转换为 OpenAI SDK 客户端构造参数。

    两个参数传 None 时返回空字典，客户端继续使用 SDK 默认值（600 秒超时、
    2 次自动重试），因此脚本生成等常规链路行为保持不变；只有连接测试这类
    需要快速失败的调用方显式传入有界值时，请求预算才会被收紧。
    """
    options = {}
    if request_timeout is not None:
        options["timeout"] = request_timeout
    if max_retries is not None:
        options["max_retries"] = max_retries
    return options


def _explain_connection_test_failure(error_message: str) -> str:
    """为超时/连接类失败补充排查方向，而不是只显示一句 SDK 原始文案。

    issue #1642 中用户的 API 本身可用，失败原因是本机到端点的链路问题
    （代理、防火墙或 DNS），但 "Request timed out." 没有指向任何可能原因。
    其它错误（鉴权、配额等）保持原文返回。
    """
    normalized = (error_message or "").lower()
    if not any(marker in normalized for marker in _CONNECTION_FAILURE_MARKERS):
        return error_message
    return (
        f"{error_message} (the request did not complete within "
        f"{LLM_CONNECTION_TEST_TIMEOUT_SECONDS:.0f}s). Please check that the "
        "configured base URL is reachable from this machine, and that no proxy "
        "or firewall is blocking the request."
    )


def _extract_qwen_generation_text(response) -> str:
    """
    从 DashScope Generation 响应中提取文本。

    Qwen 使用 `messages` 调用时返回的是 chat 结构：
    `output.choices[0].message.content`；旧 completion 形态才会返回
    `output.text`。这里两个路径都兼容，避免 `output.text` 为 None 时
    继续 `.replace()` 触发不可诊断的 AttributeError。
    """
    output = _get_response_field(response, "output")
    choices = _get_response_field(output, "choices") if output else None
    if choices is not None:
        if not choices:
            logger.warning("Qwen returned an empty choices list")
            raise ValueError("[qwen] returned empty choices")

        first_choice = choices[0]
        message = _get_response_field(first_choice, "message")
        content = _get_response_field(message, "content") if message else None
        if content is not None:
            return _normalize_text_response(content, "qwen")

    text = _get_response_field(output, "text") if output else None
    return _normalize_text_response(text, "qwen")


def llm_provider_ids() -> list[str]:
    """Every provider id, for an argparse `choices` list or an error message."""
    return sorted(LLM_PROVIDERS)


def resolve_llm_app_config(
    llm_provider: str | None = "",
    llm_model: str | None = "",
    app_config=None,
    *,
    require_credentials: bool = False,
) -> dict:
    """A config snapshot carrying one run's provider and model.

    The provider used to be `[app] llm_provider` and nothing else, so choosing
    per video meant editing config between runs — and two overlapping tasks
    could not disagree. This returns a *copy* of the config with the choice
    written into it, which `generate_script` and `generate_terms` already know
    how to take as `app_config`. Nothing global is mutated, so concurrent tasks
    stay independent.

    Empty `llm_provider` means "whatever the config says", which is what the
    CLI, the API and the WebUI all send when the user expressed no preference.
    `llm_model` overrides that provider's `*_model_name` the same way.

    `require_credentials` runs the provider's own key and model-name rules now
    rather than at the first token, so a run that cannot possibly write a
    script fails at the preflight instead of after the pictures are paid for.
    """
    base = app_config if app_config is not None else config.app
    # snapshot_config_with_pending rather than dict(): config.app is a
    # synchronized section, and this also folds in a provider the WebUI has
    # just selected but not yet saved.
    snapshot = (
        dict(base) if isinstance(base, dict) else config.snapshot_config_with_pending(base)
    )

    requested = str(llm_provider or "").strip().lower()
    if not requested:
        # Absent or blank in config means "no preference", not "no provider":
        # a config.toml written before llm_provider existed, or with the value
        # cleared, should still get the default rather than an error.
        requested = (
            str(snapshot.get("llm_provider", "") or "").strip().lower()
            or DEFAULT_LLM_PROVIDER_ID
        )

    provider = get_llm_provider(requested)
    if provider is None:
        raise ValueError(
            f'"{requested}" is not a known llm provider. '
            f"Available: {', '.join(llm_provider_ids())}."
        )
    # Always written, even when it came from the config: the snapshot is what
    # the rest of the run reads, and a snapshot that only *implies* its
    # provider leaves `unload_ollama_model` and the logs guessing.
    snapshot["llm_provider"] = provider.provider_id

    model = str(llm_model or "").strip()
    if model:
        snapshot[provider.config_key("model_name")] = model

    if require_credentials:
        _require_llm_credentials(provider, snapshot)
    return snapshot


def _require_llm_credentials(provider, snapshot: dict) -> None:
    """Fail now if this provider cannot possibly be billed to anyone.

    Only the API key, and only for providers that need one. The point of
    checking early is that a *missing key* is cheap to detect and expensive to
    discover late — a run that dies on it after the narration and the pictures
    has wasted real time. The wording matches what `_generate_response` would
    have said, so an early failure reads like the one it prevented.

    Deliberately not checked here: `model_name` and `base_url`. They are
    settings, not credentials, and `_generate_response` already reports both
    clearly at the point of use. Insisting on them here would fail every task
    on a fresh install — where `ollama_model_name` is empty until the user
    picks one — including tasks that never reach the model because the caller
    supplied the script, and tasks whose LLM is stubbed out.
    """
    if provider.requires_api_key and not str(
        snapshot.get(provider.config_key("api_key"), "") or ""
    ).strip():
        raise ValueError(
            f"{provider.provider_id}: api_key is not set, "
            "please set it in the config.toml file."
        )


def resolved_llm_provider(app_config=None) -> str:
    """The provider id a snapshot will actually use."""
    base = app_config if app_config is not None else config.app
    return str(base.get("llm_provider", DEFAULT_LLM_PROVIDER_ID) or "").lower()


def unload_ollama_model(app_config=None) -> bool:
    """Ask Ollama to drop its model, so the next stage has the card.

    Z-Image-Turbo needs about 13 GB resident and a 14B model holds about 10 GB:
    on a 16 GB card they do not fit together, and the stage that loses the race
    either spills to host memory or dies on a small allocation with gigabytes
    nominally free. `keep_alive: 0` is Ollama's own way of saying "now".

    Only for Ollama. A hosted provider holds nothing here, and calling this
    after a Claude run would unload a model this run never used.

    Returns whether a request was sent. Never raises: the next stage may well
    have room anyway, and a failed tidy-up is not a failed video.
    """
    if resolved_llm_provider(app_config) != "ollama":
        return False

    base = app_config if app_config is not None else config.app
    model_name = str(base.get("ollama_model_name", "") or "").strip()
    if not model_name:
        return False

    base_url = str(base.get("ollama_base_url", "") or "").strip()
    if not base_url:
        base_url = config.get_default_ollama_base_url()
    # ollama_base_url points at the OpenAI-compatible surface (".../v1"); the
    # unload lives on Ollama's native API next to it.
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]

    try:
        response = requests.post(
            f"{root}/api/generate",
            json={"model": model_name, "keep_alive": 0},
            timeout=30,
        )
        logger.info(
            f"asked ollama to unload {model_name} (status {response.status_code})"
        )
        return True
    except Exception as e:
        logger.warning(
            f"could not ask ollama to unload {model_name} ({type(e).__name__}); "
            "the next stage may have less VRAM than it wants"
        )
        return False


def _generate_response(
    prompt: str,
    app_config=None,
    request_timeout: float | None = None,
    max_retries: int | None = None,
) -> str:
    sdk_client = None
    sdk_stream = None
    try:
        # WebUI 在视频生成期间允许用户准备下一条文案。调用方可以传入提交瞬间
        # 的配置快照，确保模型请求重试期间不会因为后台任务结束并应用新配置，
        # 而切换到另一个 Provider、Base URL 或模型。
        runtime_app_config = app_config if app_config is not None else config.app
        llm_provider = str(
            runtime_app_config.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
        ).lower()
        provider = get_llm_provider(llm_provider)
        if provider is None:
            raise ValueError(f"{llm_provider}: unsupported llm provider")

        logger.info(f"llm provider: {llm_provider}")
        api_key = runtime_app_config.get(provider.config_key("api_key"), "")
        configured_model = runtime_app_config.get(provider.config_key("model_name"), "")
        model_name = provider.resolve_model_name(configured_model)
        if configured_model and model_name != configured_model:
            logger.warning(
                f"{llm_provider} model '{configured_model}' is deprecated, "
                f"fallback to '{model_name}'"
            )
        configured_base_url = runtime_app_config.get(
            provider.config_key("base_url"), ""
        )
        base_url = provider.resolve_base_url(configured_base_url)
        if configured_base_url and configured_base_url.strip().rstrip("/") in {
            url.rstrip("/") for url in provider.deprecated_base_urls
        }:
            logger.warning(
                f"{llm_provider} base URL '{configured_base_url}' is deprecated, "
                f"fallback to '{base_url}'"
            )
        adapter = provider.adapter
        api_version = ""

        # Ollama 的默认地址依赖当前是否运行在容器中，无法作为静态 Registry
        # 值保存；Registry 仍负责模型和必填规则，运行环境差异在这里解析。
        if llm_provider == "ollama":
            api_key = "ollama"
            if not base_url:
                base_url = config.get_default_ollama_base_url()

        if adapter == "azure":
            api_version = runtime_app_config.get(
                provider.config_key("api_version"), "2024-02-15-preview"
            )

        extra_values = {
            field.config_suffix: _resolve_provider_field_value(
                runtime_app_config.get(provider.config_key(field.config_suffix)),
                field.default_value,
            )
            for field in provider.extra_fields
        }

        if provider.requires_api_key and not api_key:
            raise ValueError(
                f"{llm_provider}: api_key is not set, please set it in the config.toml file."
            )
        if provider.requires_model_name and not model_name:
            raise ValueError(
                f"{llm_provider}: model_name is not set, please set it in the config.toml file."
            )
        if provider.requires_base_url and not base_url:
            raise ValueError(
                f"{llm_provider}: base_url is not set, please set it in the config.toml file."
            )

        for field in provider.extra_fields:
            if field.required and not extra_values[field.config_suffix]:
                raise ValueError(
                    f"{llm_provider}: {field.config_suffix} is not set, "
                    "please set it in the config.toml file."
                )

        if adapter == "qwen":
            import dashscope
            from dashscope.api_entities.dashscope_response import GenerationResponse

            response = trace_sdk_call(
                llm_provider, base_url, dashscope.Generation.call,
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                api_key=api_key,
            )
            if response:
                if isinstance(response, GenerationResponse):
                    status_code = response.status_code
                    if status_code != 200:
                        raise Exception(
                            f'[{llm_provider}] returned an error response: "{response}"'
                        )

                    return _extract_qwen_generation_text(response)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}"'
                    )
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        if adapter == "gemini":
            from google import genai
            from google.genai import types

            http_options = types.HttpOptions(base_url=base_url) if base_url else None
            generation_config = types.GenerateContentConfig(
                temperature=0.5,
                top_p=1,
                top_k=1,
                max_output_tokens=2048,
                safety_settings=[
                    types.SafetySetting(
                        category="HARM_CATEGORY_HARASSMENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_HATE_SPEECH",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                ],
            )

            try:
                # 新版 google-genai 通过统一 Client 暴露模型服务。上下文管理器
                # 会在请求结束后关闭底层 HTTP 连接，避免频繁生成时积累连接资源。
                with genai.Client(
                    api_key=api_key,
                    http_options=http_options,
                ) as client:
                    response = trace_sdk_call(
                        llm_provider, base_url, client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=generation_config,
                    )
                generated_text = response.text
            except (AttributeError, IndexError, ValueError) as e:
                logger.warning(f"gemini returned invalid response content: {str(e)}")
                raise ValueError(f"[{llm_provider}] returned invalid response content")

            return _normalize_text_response(generated_text, llm_provider)

        if adapter == "cloudflare_ai_gateway":
            account_id = extra_values["account_id"]
            gateway_id = extra_values["gateway_id"]
            # Cloudflare 当前推荐的 AI Gateway REST API 兼容 OpenAI SDK。
            # Account ID 用于构造统一端点，Gateway ID 通过请求头选择；这里
            # 不再调用 Workers AI 的 /ai/run/{model} 专用接口。
            client = sdk_client = OpenAI(
                api_key=api_key,
                base_url=(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
                ),
                default_headers={"cf-aig-gateway-id": gateway_id},
                **_sdk_transport_options(request_timeout, max_retries),
            )
            response = trace_sdk_call(
                llm_provider, base_url, client.chat.completions.create,
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "litellm":
            import litellm

            if not model_name:
                raise ValueError(
                    f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                )

            response = trace_sdk_call(
                llm_provider, base_url, litellm.completion,
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                drop_params=True,
            )

            if not response:
                raise ValueError(f"[{llm_provider}] returned empty response")
            if not getattr(response, "choices", None):
                raise ValueError(f"[{llm_provider}] returned empty response")

            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "azure":
            # Azure OpenAI SDK 使用 `azure_endpoint` 和 `api_version` 生成专用请求地址，
            # 不能继续复用下面普通 OpenAI-compatible 的 `base_url` 初始化逻辑。
            # 这里在 Azure 分支内完成请求并立即返回，避免客户端被后续 fallback
            # 覆盖，导致用户配置的 Azure 凭证通过校验但实际请求没有被使用。
            logger.info(f"requesting azure chat completion, model: {model_name}")
            client = sdk_client = AzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=base_url,
                **_sdk_transport_options(request_timeout, max_retries),
            )
            response = trace_sdk_call(
                llm_provider, base_url, client.chat.completions.create,
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    return _extract_chat_completion_text(response, llm_provider)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        if adapter in {"openai_codex", "openai_chatgpt"}:
            if adapter == "openai_chatgpt":
                from app.services import chatgpt_oauth as subscription_provider
            else:
                from app.services import codex_oauth as subscription_provider

            timeout_seconds = coerce_claude_code_timeout(
                extra_values.get("timeout"), provider.config_key("timeout")
            )
            if request_timeout is not None:
                timeout_seconds = min(timeout_seconds, request_timeout)
            return _normalize_text_response(
                subscription_provider.generate_text(prompt, model_name, timeout_seconds),
                llm_provider,
            )

        if adapter == "claude_code":
            # Claude 订阅（Pro / Max / Team）不签发 API Key，其凭证只能由
            # Claude Code 官方客户端自己使用。这里不直接请求 Anthropic API，
            # 而是以 headless 模式调用本机已登录的 claude CLI（`claude -p`），
            # 由 CLI 完成鉴权，脚本生成只消费它返回的文本。
            configured_cli = (extra_values.get("cli_path") or "").strip() or "claude"
            cli_path = shutil.which(configured_cli)
            if not cli_path and os.path.isfile(configured_cli):
                cli_path = configured_cli
            if not cli_path:
                raise ValueError(
                    f"{llm_provider}: claude CLI not found ('{configured_cli}'), "
                    f"install it in the runtime or set "
                    f"{provider.config_key('cli_path')} in the config.toml file."
                )

            try:
                timeout_seconds = coerce_claude_code_timeout(
                    extra_values.get("timeout"), provider.config_key("timeout")
                )
            except ValueError as timeout_error:
                raise ValueError(f"{llm_provider}: {timeout_error}") from None

            # prompt 通过 stdin 传入，不放在命令行里：Windows 上 npm 安装的
            # claude 是 claude.cmd，cmd.exe 会在第一个换行处截断参数，多行
            # prompt 和其后的隔离参数都会丢失。
            command = [
                cli_path,
                "-p",
                "--output-format",
                "json",
                "--system-prompt",
                CLAUDE_CODE_SYSTEM_PROMPT,
                # 关闭全部内置工具，保证只做文本生成。
                "--tools",
                "",
                # 关闭 CLAUDE.md、skills、hooks、plugins、MCP 等用户级定制；
                # 鉴权与模型选择不受影响（不能用 --bare，它会禁用 OAuth）。
                "--safe-mode",
            ]
            # 模型名留空时沿用 CLI 自己的默认模型，避免这里硬编码的模型 ID
            # 随订阅可用模型变化而失效。
            if model_name:
                command += ["--model", model_name]

            cli_env, removed_env = build_claude_code_env()
            if removed_env:
                # 只记录变量名，不记录取值，避免把密钥写进日志。
                logger.warning(
                    f"{llm_provider}: ignoring conflicting environment variables "
                    f"so the subscription login is used: {', '.join(removed_env)}"
                )

            logger.info(f"invoking claude cli, model: {model_name or 'cli default'}")
            # CLI 会读取工作目录下的 CLAUDE.md 和项目设置，这些内容会污染
            # 文案结果，因此固定在一个临时空目录中执行。
            with tempfile.TemporaryDirectory() as work_dir:
                try:
                    completed = subprocess.run(
                        command,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        # The CLI always emits UTF-8. Without an explicit encoding,
                        # text=True decodes with the system locale (e.g. cp1252 on
                        # non-English Windows), so every non-ASCII character reaches
                        # the script as mojibake.
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout_seconds,
                        cwd=work_dir,
                        env=cli_env,
                    )
                except subprocess.TimeoutExpired:
                    raise Exception(
                        f"[{llm_provider}] claude cli timed out after "
                        f"{timeout_seconds:.0f}s"
                    )

            # 未登录、用量耗尽这类失败同样会返回 JSON（`is_error` 为真，
            # `result` 是可读原因），只是退出码非 0。因此先解析 stdout，
            # 只有在拿不到 JSON 时才回退到退出码和 stderr。
            stdout = (completed.stdout or "").strip()
            try:
                payload = json.loads(stdout) if stdout else None
            except json.JSONDecodeError:
                payload = None

            if payload is None:
                detail = (completed.stderr or stdout or "").strip()
                if "unknown option" in detail.lower():
                    raise Exception(
                        f"[{llm_provider}] the installed claude CLI does not support "
                        f"the required isolation flags; upgrade to "
                        f"{CLAUDE_CODE_MIN_CLI_VERSION} or newer: {detail[:300]}"
                    )
                if completed.returncode != 0:
                    raise Exception(
                        f"[{llm_provider}] claude cli exited with code "
                        f"{completed.returncode}: {detail[:500]}"
                    )
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{detail[:500]}"'
                )

            if payload.get("is_error") or completed.returncode != 0:
                reason = str(payload.get("result") or "").strip() or (
                    f"claude cli exited with code {completed.returncode}"
                )
                # 容器里无法执行交互式 /login，这里直接给出可用的鉴权方式。
                if "login" in reason.lower():
                    reason += (
                        " (run `claude setup-token` on the host and pass the token "
                        "to the container as CLAUDE_CODE_OAUTH_TOKEN)"
                    )
                raise Exception(
                    f'[{llm_provider}] returned an error response: "{reason[:500]}"'
                )

            return _normalize_text_response(payload.get("result"), llm_provider)

        if adapter == "kimi_code":
            # Kimi 订阅（Plus+ 的 Kimi Code 权益）不签发按量 API Key，鉴权由
            # OAuth 设备授权完成：WebUI 引导用户在浏览器里一键确认，本地保存
            # refresh token；access token 15 分钟过期，由 kimi_code_oauth 透明
            # 续期。请求走官方 OpenAI 兼容端点，与按量 API 是两套独立额度。
            from app.services import kimi_code_oauth

            try:
                timeout_seconds = kimi_code_oauth.coerce_timeout(
                    extra_values.get("timeout")
                )
            except ValueError as timeout_error:
                raise ValueError(f"{llm_provider}: {timeout_error}") from None

            access_token = kimi_code_oauth.get_valid_access_token()
            # 授权区域以凭证为准：token 只对签发它的区域有效。用户在授权后切换
            # API Platform 时，若按界面区域发请求会把中国签发的 token 打到
            # Global 端点（必然 401），因此请求地址优先跟随 token 的签发区域。
            authorized_region = kimi_code_oauth.authorized_region()
            if authorized_region:
                request_base_url = kimi_code_oauth.api_base_url(authorized_region)
                if base_url and authorized_region != kimi_code_oauth.resolve_region(
                    base_url
                ):
                    logger.warning(
                        f"kimi_code: API Platform selection differs from the "
                        f"authorized region; using '{authorized_region}' to match "
                        "the issued token"
                    )
            else:
                request_base_url = base_url or kimi_code_oauth.api_base_url(
                    kimi_code_oauth.resolve_region(base_url)
                )
            # 模型名留空时默认 kimi-for-coding：订阅权益内的专用编码模型，
            # 避免硬编码的通用模型 id 随权益变化而失效。
            request_model = model_name or "kimi-for-coding"
            logger.info(f"requesting kimi code chat completion, model: {request_model}")
            # Provider 自身的宽松超时在连接测试场景下要收紧到测试预算以内。
            effective_timeout = timeout_seconds
            if request_timeout is not None:
                effective_timeout = min(effective_timeout, request_timeout)
            client = sdk_client = OpenAI(
                api_key=access_token,
                base_url=request_base_url,
                timeout=effective_timeout,
                # 服务端反爬管线要求与 CLI 一致的设备身份头。
                default_headers=kimi_code_oauth.api_headers(),
                **_sdk_transport_options(None, max_retries),
            )
            response = trace_sdk_call(
                llm_provider, base_url, client.chat.completions.create,
                model=request_model, messages=[{"role": "user", "content": prompt}]
            )
            if response and isinstance(response, ChatCompletion):
                return _extract_chat_completion_text(response, llm_provider)
            raise Exception(
                f'[{llm_provider}] returned an invalid response: "{response}"'
            )

        if adapter == "modelscope":
            content = ""
            client = sdk_client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                **_sdk_transport_options(request_timeout, max_retries),
            )
            response = sdk_stream = trace_sdk_call(
                llm_provider, base_url, client.chat.completions.create,
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"enable_thinking": False},
                stream=True,
            )
            if response:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content += delta.content

                if not content.strip():
                    raise ValueError("Empty content in stream response")

                return _normalize_text_response(content, llm_provider)
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        client = sdk_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            **_sdk_transport_options(request_timeout, max_retries),
        )

        response = trace_sdk_call(
            llm_provider, base_url, client.chat.completions.create,
            model=model_name, messages=[{"role": "user", "content": prompt}]
        )
        if response:
            if isinstance(response, ChatCompletion):
                return _extract_chat_completion_text(response, llm_provider)
            else:
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                    f"connection and try again."
                )
        else:
            raise Exception(
                f"[{llm_provider}] returned an empty response, please check your network connection and try again."
            )

    except Exception as e:
        return f"Error: {_sanitize_error_message(e)}"

    finally:
        for resource in (sdk_stream, sdk_client):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as cleanup_error:
                    logger.warning(
                        f"could not close LLM transport: {type(cleanup_error).__name__}"
                    )


def test_connection() -> tuple[bool, str, float]:
    """
    使用当前 Provider 配置发起一次最小请求，验证实际生成链路是否可用。

    连接测试直接复用 `_generate_response()`，因此会覆盖 API Key、Base URL、
    模型名称和 Provider 专用字段，但不会进入脚本生成的重试逻辑，也不会发送
    用户的视频主题或文案。请求使用有界的短超时并禁用 SDK 自动重试：端点
    不可达（代理、防火墙或 DNS 故障）时快速失败，并附上排查方向，而不是
    挂起数分钟后只留下 "Request timed out."（issue #1642）。返回值依次为
    成功状态、错误信息和请求耗时。
    """
    started_at = perf_counter()
    response = _generate_response(
        prompt="Reply with exactly: OK",
        request_timeout=LLM_CONNECTION_TEST_TIMEOUT_SECONDS,
        max_retries=LLM_CONNECTION_TEST_MAX_RETRIES,
    )
    elapsed = perf_counter() - started_at

    if not response:
        error_message = "LLM returned an empty response"
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    if response.startswith("Error:"):
        error_message = _explain_connection_test_failure(
            response.removeprefix("Error:").strip()
        )
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    logger.info(f"llm connection test succeeded, elapsed: {elapsed:.2f}s")
    return True, "", elapsed


def _limit_script_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层已经用 Pydantic 做长度校验；这里继续兜底，是为了保护
    # WebUI 或内部服务直接调用 generate_script 时不会把超长提示词发送给模型，
    # 避免 token 成本异常和请求失败。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _normalize_script_paragraph_number(paragraph_number: int | None) -> int:
    try:
        value = int(paragraph_number or MIN_SCRIPT_PARAGRAPH_NUMBER)
    except (TypeError, ValueError):
        value = MIN_SCRIPT_PARAGRAPH_NUMBER

    if value < MIN_SCRIPT_PARAGRAPH_NUMBER or value > MAX_SCRIPT_PARAGRAPH_NUMBER:
        # WebUI 和 API 都会限制范围；这里兜底处理内部调用，避免异常参数直接扩大
        # LLM 生成成本或生成空结果。
        logger.warning(
            f"script paragraph_number is out of range and will be clamped: {value}"
        )
        return max(MIN_SCRIPT_PARAGRAPH_NUMBER, min(value, MAX_SCRIPT_PARAGRAPH_NUMBER))

    return value


def build_script_prompt(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )

    # 将“脚本生成规则”和“运行时上下文”分开拼接。这样高级用户即使覆盖默认
    # system prompt，也不会漏掉视频主题、语言、段落数这些每次生成都必须带上的参数。
    prompt = custom_system_prompt or DEFAULT_SCRIPT_SYSTEM_PROMPT
    prompt += f"""

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}
""".rstrip()
    if language:
        prompt += f"\n- language: {language}"
    if video_script_prompt:
        prompt += f"""

# Additional User Requirements:
{video_script_prompt}
""".rstrip()

    return prompt


def generate_script(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    app_config=None,
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    prompt = build_script_prompt(
        video_subject=video_subject,
        language=language,
        paragraph_number=paragraph_number,
        video_script_prompt=video_script_prompt,
        custom_system_prompt=custom_system_prompt,
    )
    final_script = ""
    logger.info(
        "generating video script: "
        f"subject={video_subject}, paragraph_number={paragraph_number}, "
        f"has_custom_prompt={bool(video_script_prompt.strip())}, "
        f"has_custom_system_prompt={bool(custom_system_prompt.strip())}"
    )

    def format_response(response):
        # Clean the script
        # Remove emphasis and heading markers without changing inline names
        # or musical notes such as C#, F# and G#.
        response = response.replace("*", "")
        response = re.sub(
            r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*$",
            lambda match: re.sub(r"[ \t]+#+[ \t]*$", "", match.group(1)),
            response,
        )

        # Link labels are narration; discard destinations before stripping
        # bracketed stage directions. Common parenthesized URL components fit.
        response = re.sub(
            r"\[([^\]\n]+)\]\((?:[^()\n]|\([^()\n]*\))*\)",
            lambda match: match.group(1),
            response,
        )

        # Remove markdown syntax.  Use non-greedy .*? so each bracket/paren
        # group is removed independently; the greedy form would eat all text
        # between the first opener and the last closer on the same line.
        response = re.sub(r"\[.*?\]", "", response)
        response = re.sub(r"\(.*?\)", "", response)

        # Split the script into paragraphs
        paragraphs = response.split("\n\n")

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraphs)

    for i in range(_max_retries):
        try:
            if app_config is None:
                response = _generate_response(prompt=prompt)
            else:
                response = _generate_response(prompt=prompt, app_config=app_config)
            if isinstance(response, str) and response.startswith("Error: "):
                # _generate_response returns provider failures as text. Passing
                # that text through would make the task treat it as narration.
                raise ValueError(response)
            if response:
                candidate = format_response(response).strip()
            else:
                logging.error("gpt returned an empty response")
                candidate = ""

            # Some upstream providers may return quota errors as plain text.
            if candidate and "当日额度已消耗完" in candidate:
                raise ValueError(candidate)

            if candidate:
                final_script = candidate
                break
        except Exception as e:
            logger.error(f"failed to generate script: {e}")

        if i < _max_retries - 1:
            logger.warning(f"failed to generate video script, trying again... {i + 1}")
    if not final_script:
        logger.error("failed to generate video script after retries")
    else:
        logger.success(f"completed: \n{final_script}")
    return final_script.strip()


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Non-OpenAI providers (Claude, Gemini, …) frequently wrap JSON output in a
    ```json … ``` fence even when asked to return raw JSON. Removing it lets the
    first json.loads() succeed instead of falling through to the regex recovery
    path (and spuriously logging a warning). Mirrors the DOTALL handling already
    used in _parse_social_metadata().
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _extract_embedded_json(text: str, opening: str):
    """Decode one complete JSON value without absorbing later prose/examples."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(text or ""):
        if character != opening:
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("response contains no complete JSON value")


def generate_terms(
    video_subject: str,
    video_script: str,
    amount: int = 5,
    match_script_order: bool = False,
    app_config=None,
) -> List[str]:
    video_script = utils.remove_pause_tags(video_script or "").strip()
    if match_script_order:
        goal = (
            f"Generate {amount} chronological stock-video search terms that follow "
            "the order of topics in the video script."
        )
        ordering_rule = (
            "6. keep the terms in the same order as the script narration; "
            "earlier terms must describe earlier visual moments."
        )
        # 有序关键词模式下，示例数量要和 amount 保持一致，避免模型被固定
        # 的 4 个示例误导，导致长文案只返回少量关键词，影响素材覆盖度。
        example_terms = [
            "opening visual topic",
            *[f"script visual topic {index}" for index in range(2, max(amount, 1))],
            "final visual topic",
        ]
        output_example = json.dumps(example_terms[:amount], ensure_ascii=False)
    else:
        goal = (
            f"Generate {amount} search terms for stock videos, depending on the "
            "subject of a video."
        )
        ordering_rule = ""
        output_example = (
            '["search term 1", "search term 2", "search term 3",'
            '"search term 4", "search term 5"]'
        )

    prompt = f"""
# Role: Video Search Terms Generator

## Goals:
{goal}

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with english search terms only.
{ordering_rule}

## Output Example:
{output_example}

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use English for generating video search terms; Chinese is not accepted.
""".strip()

    logger.info(f"subject: {video_subject}, match_script_order: {match_script_order}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        search_terms = []
        try:
            if app_config is None:
                response = _generate_response(prompt)
            else:
                response = _generate_response(prompt, app_config=app_config)
            if response.startswith("Error: "):
                # generate_terms 的公开返回类型是 List[str]。如果把 Provider 的
                # 错误文案原样返回，下游只做空值判断时会把非空字符串误认为成功，
                # 素材下载循环还会按字符遍历错误文案，产生无意义的外部请求。
                # 这里统一返回空列表，让任务编排层在真实故障位置立即结束任务。
                logger.error(f"failed to generate video terms: {response}")
                return []
            search_terms = json.loads(_strip_code_fence(response))
        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                try:
                    search_terms = _extract_embedded_json(response, "[")
                except ValueError as e:
                    logger.warning(f"failed to generate video terms: {str(e)}")

        # Apply the same contract to direct JSON and prose-wrapped recovery.
        # Otherwise a nonempty array of numbers or objects reaches material search.
        if not isinstance(search_terms, list) or not all(
            isinstance(term, str) for term in search_terms
        ):
            logger.error("response is not a list of strings.")
            search_terms = []

        # The model may ignore the requested count or return blank strings.
        # Enforce the prompt contract before material providers are contacted;
        # repeated topics remain meaningful in chronological mode.
        search_terms = [term.strip() for term in search_terms if term.strip()][:amount]
        if search_terms:
            break
        if i < _max_retries - 1:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms


def _scene_search_term_request(subject: str, numbered: str, count: int) -> str:
    """逐句要一个"拿去搜素材"的短关键词。

    素材库搜索吃的是短查询：把一整句画面描述丢给 Pexels 只会搜不到东西，
    所以库存素材那条路仍然要短词。
    """
    return f"""
# Role: Scene Image Prompt Generator

## Goals:
For each numbered narration line, write one short English search term
describing the picture that should be on screen **while that exact line is
spoken**.

## Constrains:
1. return a json-array of strings and nothing else.
2. the array must have exactly {count} items, in the same order as the lines.
3. item i must depict what line i is talking about, not the video as a whole.
4. 2-6 words, concrete and depictable: name the subject and what it is doing.
   prefer "a woman kneading dough on a wooden table" over "food preparation".
5. no camera directions, no text overlays, no abstract nouns on their own.
6. english only.

## Output Example:
["a cracked human jawbone on dark soil", "a dry riverbank in northern syria"]

## Context:
### Video Subject
{subject}

### Narration Lines
{numbered}
""".strip()


def _style_guidance(style: str) -> str:
    """把用户选的画面风格告诉写描述的模型。

    为什么需要它
    ------------
    风格词是拼在描述后面的,两边必须讲同一件事。火柴人那套预设要的是"一个
    小人正在做某件事"——"round white head, thin black line arms and legs,
    clearly holding the objects of the action, mid-action pose"。如果描述写
    的是"大脑扫描图高亮视觉皮层",两半就在互相打架,画出来的东西两头不像。

    这里把风格原文交给模型，而不是针对某个预设写死规则：用户自己填的模板
    同样能被照顾到,新增预设也不需要再改这里。

    The style words are appended to the description, so the two halves have to
    describe the same picture. The stick-figure preset wants a little person
    performing an action; a description about a brain scan fights it. Handing
    the model the style text itself covers custom templates and new presets
    without a rule per preset.
    """
    text = " ".join(str(style or "").replace("{term}", " ").split()).strip(" ,")
    if not text:
        return ""
    return f"""
### Picture Style
Every picture is rendered in this style, which is appended to your text:
{text}

Choose what to depict so that it suits that style. If the style describes
drawn characters or figures, put a person in frame doing something visible
rather than a diagram, a scan or an abstract view. Do not repeat these style
words in your own text.
"""


def _scene_image_prompt_request(
    subject: str, numbered: str, count: int, style: str = ""
) -> str:
    """逐句写一句完整的画面描述，直接拿去给文生图模型。

    为什么不是短关键词
    ------------------
    短关键词是为素材库搜索准备的。文生图模型拿到的如果只有 "dry eye" 加一串
    风格词，它既看不到这一句在讲什么，也看不到整条片子在讲什么，于是画出来
    的东西经常属于别的段落。这里把"视频主题"和"这一句旁白"直接交给模型，让
    它写出这一刻该看到的画面，那句描述就是最终提示词。

    断句是按标点切的，所以不少句子本身画不出来——"Left untreated"、
    "Keep going for hours"。因此这里明确要求：画不出来的句子要结合主题推断
    出一个具体场面，而不是把那几个字直接当画面。

    Short keywords exist for stock-footage search. An image model given only
    "dry eye" plus style words cannot see what this line says or what the
    video is about, so it draws something belonging to another section. The
    subject and the line go to the model directly, and the sentence it writes
    becomes the prompt. Punctuation-split lines are often not depictable on
    their own, so the model is told to infer a concrete scene from the subject
    rather than illustrate the words literally.

    为什么规则里要强调人、表情和时代
    --------------------------------
    这几条是从用户手改的提示词里学来的。同一条片子里，模型自己给的是
    "bowl of rice"、"rice terraces"、"ancient stone tools"——没有人、没有动作、
    没有年代，画出来就是一张张库存图，摆在哪一段都成立，也就等于什么都没讲。
    用户改成 "The asian farmer makes a very angry face to the rice"、"asian
    people from ten thousand years ago cutting wild rice with small stone
    blades"、"Ancient Asian farmer selecting rice seeds to plant" 之后，画面
    立刻开始替旁白讲故事。三点差别：

    * **有人在做事**，而不是一件东西摆在那里；
    * **情绪长在脸上**："难收"这句话配的是一张恼火的脸,而不是一株稻子;
    * **年代和地点每一条都重写一遍**。每张图是分开画的,彼此没有记忆,所以
      "一万年前"、"中国南方"不写进这一条,这一条就会画成现代。

    These rules were learned from prompts a user rewrote by hand. The model's
    own terms were objects with nobody in them, which read as stock imagery
    that could sit under any line. The rewrites named a person, an action, a
    feeling and a period, and the pictures began doing narrative work.
    """
    return f"""
# Role: Scene Image Prompt Writer

## Goals:
For each numbered narration line, write one English image-generation prompt
describing exactly what the viewer should see on screen **while that line is
spoken**, in a video about the subject below. The pictures have to carry the
story, not merely decorate it: someone watching with the sound off should be
able to follow what is happening from the pictures alone.

## Constrains:
1. return a json-array of strings and nothing else.
2. the array must have exactly {count} items, in the same order as the lines.
3. item i must show what line i is saying, read in the context of the subject
   and of the lines around it - not the video as a whole.
4. one sentence, roughly 15-35 words. concrete and physically depictable.
5. **show a person or people doing something.** name who they are and the
   action they are taking. a bare object with nobody in frame ("a bowl of
   rice", "stone tools", "terraced fields") is a stock photo, not a story
   beat - use one only when the line is genuinely about that object alone,
   and even then show a hand or a person interacting with it.
6. **when the line carries a feeling or a judgement, put it on a face.** lines
   like "terrible for anyone trying to harvest it", "nobody expected this" or
   "it changed everything" should show someone reacting - frustrated,
   astonished, exhausted, delighted - not an illustration of the noun in the
   sentence. the expression is what tells the viewer how to feel about the
   fact.
7. **repeat the setting in every prompt.** each picture is drawn separately,
   with no memory of the others, so any period, place, culture or season the
   narration establishes anywhere must be restated in every prompt where
   people, clothing or landscape appear - "ten thousand years ago", "ancient",
   "in southern China", "pre-industrial", and so on. leaving it out gives a
   modern street scene in the middle of a stone-age story.
8. **keep recurring people recognisably the same.** if the video follows one
   farmer, inventor or patient, describe them the same way each time (age,
   build, clothing) so the pictures read as one story rather than a slideshow
   of strangers.
9. a line that cannot be drawn on its own ("Left untreated", "Year after
   year", "Keep going for hours") still gets a concrete scene: infer from the
   subject and the neighbouring lines what is happening at that moment and
   describe that, with people in it.
10. do not quote or spell out the narration, and never ask for text, captions,
    numbers or letters inside the picture.
11. no camera directions, no style words, no lighting or render instructions -
    those are appended separately from the user's chosen style.
12. english only.

## Output Example:
Subject: "How rice farming was invented"; the narration establishes southern
China about ten thousand years ago.
["a wild rice plant heavy with ripe grain in a marsh in southern China ten
  thousand years ago, seeds already scattering from the stalk",
 "an ancient Chinese farmer in a straw hat crouched in the marsh, face tight
  with frustration as the ripe grains fall from his hands",
 "the same ancient farmer and two others cutting wild rice with small stone
  blades, barefoot in the shallow water of a prehistoric paddy",
 "an ancient Chinese farmer sorting the heaviest seed heads into a woven
  basket, picking out the ones still clinging to the stalk"]

## Context:
### Video Subject
{subject}
{_style_guidance(style)}
### Narration Lines
{numbered}
""".strip()


def _one_scene_image_prompt_request(
    subject: str, narration: str, keyword: str, style: str = ""
) -> str:
    """重出一张图时，把主题、这一段念的话和用户的关键词一起交给模型。"""
    steer = (
        f"\n### What the user asked to see\n{keyword}\n"
        if str(keyword or "").strip()
        else ""
    )
    spoken = str(narration or "").strip() or "(this scene has no narration)"
    return f"""
# Role: Scene Image Prompt Writer

## Goals:
Write ONE English image-generation prompt for a single scene of a video about
the subject below. The picture has to carry what is being said at this moment,
not merely decorate it.

## Constrains:
1. reply with the prompt sentence only - no json, no quotes, no preamble.
2. one sentence, roughly 15-35 words, concrete and physically depictable.
3. show what the narration below is saying, read in the context of the subject.
4. **show a person or people doing something.** name who they are and the
   action they are taking. a bare object with nobody in frame is a stock photo,
   not a story beat.
5. **when the narration carries a feeling or a judgement, put it on a face** -
   frustrated, astonished, exhausted, delighted - rather than illustrating the
   noun in the sentence.
6. **state the period, place and culture explicitly**, taking them from the
   subject and the narration ("ten thousand years ago", "ancient", "in
   southern China"). the picture is drawn with no knowledge of the rest of the
   video, so leaving this out gives a modern scene in a historical story.
7. if the user asked for something specific below, that is the instruction:
   keep their subject, their action and their mood, and add the context,
   period and place around it. do not replace their idea with your own.
8. never ask for text, captions, numbers or letters inside the picture.
9. no camera directions, no style words, no lighting or render instructions.
10. english only.

## Context:
### Video Subject
{subject}
{_style_guidance(style)}
### What is said over this scene
{spoken}{steer}
""".strip()


def generate_scene_image_prompt(
    video_subject: str,
    narration: str,
    keyword: str = "",
    app_config=None,
    *,
    image_style: str = "",
) -> str:
    """为**一段**画面写一句提示词，失败时返回空串。

    为什么需要它
    ------------
    出片那条路上，提示词是模型读着主题和整篇旁白逐句写出来的,所以画面知道
    "这一刻在讲什么"。但在审片界面里重出一张图走的是另一条路：只把关键词替
    进画风模板,主题和这一段的台词一个字都没带上(见
    ``material._openai_image_prompt``)。于是 "bowl of rice" 这种词出来的就是
    一张库存图,它摆在哪一段下面都成立,也就等于什么都没讲。

    这里把重生成也接回同一条路：主题是上下文，台词是这一刻的事，关键词是用
    户要的东西，三样一起交给模型，让它写出该看到的画面。

    The pipeline writes prompts from the subject and the whole narration, so
    the picture knows what this moment is about. Regenerating from the review
    screen did not: it substituted the keyword into the style template and sent
    nothing else. The subject, the line spoken here and the user's keyword now
    all reach the writer.
    """
    subject = str(video_subject or "").strip()
    spoken = str(narration or "").strip()
    steer = str(keyword or "").strip()
    if not subject and not spoken and not steer:
        return ""

    prompt = _one_scene_image_prompt_request(subject, spoken, steer, image_style)
    try:
        if app_config is None:
            response = _generate_response(prompt)
        else:
            response = _generate_response(prompt, app_config=app_config)
    except Exception as exc:
        logger.warning(f"failed to write a scene image prompt: {exc}")
        return ""
    if not response or response.startswith("Error: "):
        logger.warning(f"failed to write a scene image prompt: {response}")
        return ""

    # 模型偶尔会裹一层引号或者前面带一句"Here is the prompt:"。
    written = _strip_code_fence(str(response)).strip().strip('"').strip()
    written = written.split("\n")[-1].strip() if "\n" in written else written
    if not written:
        return ""
    # 写跑偏了（整段解释、或者把旁白原样抄回来）就不要，交回去用原始关键词。
    if len(written) > 600:
        logger.warning("the written scene image prompt was too long; ignoring it")
        return ""
    return written


def generate_terms_per_line(
    video_subject: str,
    narration_lines: List[str],
    app_config=None,
    *,
    visual_prompt: bool = False,
    image_style: str = "",
) -> List[str]:
    """为每一句旁白各生成一个画面关键词，返回长度与入参一致的列表。

    为什么需要它
    ------------
    ``generate_terms`` 把整篇脚本交给模型，要回**固定条数**(5 或 8)的主题词；
    下游再按下标把第 i 个词配给第 i 句旁白。两边条数根本不相等——实测一条
    任务有 24 句旁白、12 个关键词——于是"第 2 句讲的是叙利亚北部，配的却是
    考古学家检查骨骼"这种错配是必然的，而不是偶然。

    这里把问题反过来：先切好句子，让模型逐句给出"这句话该配什么画面"，
    条数由句子数决定。配对因此在构造上就是对的，不再依赖模型自己的分段
    恰好和断句一致。

    ``generate_terms`` asks for a fixed number of topics for the whole script
    and the caller then pairs term *i* with narration line *i*. The two counts
    are unrelated, so the mismatch is structural, not occasional. This asks for
    one term per line instead, which makes the pairing correct by construction.

    返回值长度**保证**等于 ``narration_lines``：模型少给就用主题兜底，多给就
    截断。下游是按下标取的，长度不对会让整条时间线错位。
    """
    lines = [str(line or "").strip() for line in narration_lines or []]
    lines = [line for line in lines if line]
    if not lines:
        return []

    subject = str(video_subject or "").strip()
    numbered = "\n".join(
        f"{index + 1}. {utils.remove_pause_tags(line)}"
        for index, line in enumerate(lines)
    )
    # 文生图那条路要的是一整句画面描述，库存素材那条路要的是短查询。
    # 同一个函数两种产物，是因为"每句一条、条数对齐"这套保证两边都要。
    prompt = (
        _scene_image_prompt_request(subject, numbered, len(lines), image_style)
        if visual_prompt
        else _scene_search_term_request(subject, numbered, len(lines))
    )

    wanted = "image prompt" if visual_prompt else "search term"
    logger.info(
        f"generating one {wanted} per narration line: {len(lines)} lines"
    )

    terms: List[str] = []
    for attempt in range(_max_retries):
        response = ""
        try:
            if app_config is None:
                response = _generate_response(prompt)
            else:
                response = _generate_response(prompt, app_config=app_config)
            if response.startswith("Error: "):
                # 与 generate_terms 一致：不要把错误文案当成关键词传下去。
                logger.error(f"failed to generate per-line terms: {response}")
                return []
            terms = json.loads(_strip_code_fence(response))
        except Exception as exc:
            logger.warning(f"failed to generate per-line terms: {exc}")
            if response:
                try:
                    terms = _extract_embedded_json(response, "[")
                except ValueError as inner:
                    logger.warning(f"failed to generate per-line terms: {inner}")

        if isinstance(terms, list) and all(isinstance(term, str) for term in terms):
            terms = [term.strip() for term in terms]
            if any(terms):
                break
        terms = []
        if attempt < _max_retries - 1:
            logger.warning(
                f"failed to generate per-line terms, trying again... {attempt + 1}"
            )

    if not terms:
        return []

    # 长度必须对齐。模型少给时用主题兜底，而不是让后面的句子整体前移——
    # 错位一格比配一个泛一点的画面糟糕得多。
    aligned: List[str] = []
    for index in range(len(lines)):
        term = terms[index].strip() if index < len(terms) else ""
        aligned.append(term or subject or lines[index])
    logger.success(f"completed: {len(aligned)} per-line {wanted}s")
    return aligned


# =============================================================================
# Social publishing metadata
#
# 根据视频主题和脚本生成发布到短视频平台时常用的 title、caption 和 hashtags。
# 这块能力只复用现有 LLM provider，不接入任何外部发布服务，也不影响视频生成主链路。
# =============================================================================

# 不同平台的文案长度和 hashtag 数量偏好不同。这里使用保守上限，避免模型返回
# 过长内容后调用方还需要二次裁剪。
SOCIAL_PLATFORMS = {
    "tiktok": {"title_max": 100, "caption_max": 2200, "hashtag_count": 5},
    "youtube_shorts": {"title_max": 100, "caption_max": 5000, "hashtag_count": 3},
    "instagram_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 8},
    "facebook_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 5},
}
DEFAULT_SOCIAL_PLATFORM = "tiktok"
DEFAULT_SOCIAL_LANGUAGE = "auto"
MAX_SOCIAL_SUBJECT_LENGTH = 500
MAX_SOCIAL_SCRIPT_LENGTH = 8000
MAX_SOCIAL_LANGUAGE_LENGTH = 64

SOCIAL_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "facebook_reels": "Facebook Reels",
}

# LLM 不可用时的通用兜底标签。这里故意不绑定某个国家或语种，保证 API
# 对中文、英文、越南语等不同场景都能返回可用结构。
DEFAULT_SOCIAL_HASHTAGS = [
    "#shorts",
    "#viral",
    "#trending",
    "#fyp",
    "#video",
    "#reels",
    "#creator",
    "#content",
]


def _resolve_social_platform(platform: str | None) -> str:
    value = (platform or "").strip().lower()
    return value if value in SOCIAL_PLATFORMS else DEFAULT_SOCIAL_PLATFORM


def _normalize_social_language(language: str | None) -> str:
    value = (language or DEFAULT_SOCIAL_LANGUAGE).strip()
    if len(value) > MAX_SOCIAL_LANGUAGE_LENGTH:
        logger.warning(
            "social metadata language is too long and will be truncated to "
            f"{MAX_SOCIAL_LANGUAGE_LENGTH} characters."
        )
        value = value[:MAX_SOCIAL_LANGUAGE_LENGTH]
    return value or DEFAULT_SOCIAL_LANGUAGE


def _limit_social_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层会限制长度；这里继续兜底，是为了保护内部调用或未来 WebUI
    # 直接调用时不会把超长内容发送给模型，避免 token 成本异常。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _social_language_instruction(language: str | None) -> str:
    language = _normalize_social_language(language)
    if language.lower() == DEFAULT_SOCIAL_LANGUAGE:
        return (
            "Use the same language as the video subject and script. If the subject "
            "and script use different languages, prefer the script language."
        )

    return f'Write "title" and "caption" in this language: {language}.'


def _clamp_text(text, max_length: int) -> str:
    value = ("" if text is None else str(text)).strip()
    if max_length and len(value) > max_length:
        return value[:max_length].rstrip()
    return value


def _normalize_hashtags(raw, count: int) -> List[str]:
    """
    将 LLM 返回的 hashtag 统一整理成 `#tag` 格式。

    LLM 可能返回字符串、数组、带空格的词组、重复标签或包含标点的内容。
    这里集中清洗，可以让接口响应结构稳定，也避免平台发布时出现空标签、
    重复标签或不符合常见格式的 hashtag。
    """
    if isinstance(raw, str):
        # CJK text separates tags with ，、； and no spaces; # starts a new tag.
        candidates = re.split(r"[\s,，、;；#]+", raw)
    elif isinstance(raw, (list, tuple)):
        # 数组里的每一项视为一个完整标签，因此 "du lich" 会变成
        # "#dulich"，而不是拆成两个标签。
        candidates = [str(entry) for entry in raw]
    else:
        candidates = []

    seen = set()
    result: List[str] = []
    for item in candidates:
        # 印地语、泰语等依赖组合符，不能像 \w 那样直接删除。但组合符必须
        # 附着于保留的字符；emoji 被删除后，其变体选择符也应丢弃，否则
        # #☕️ 会留下只有不可见字符的标签，#coffee☕️ 也无法与 #coffee 去重。
        # 数字本身会保留，因此还需清理 emoji 展示选择符和键帽组合符，
        # 让 #1️⃣ 与 #1 一致；其它语言组合符和 CJK 字形变体仍按附着规则保留。
        characters = []
        can_attach_mark = False
        for character in item:
            if character in {"\ufe0e", "\ufe0f", "\u20e3"}:
                can_attach_mark = False
            elif character.isalnum() or character == "_":
                characters.append(character)
                can_attach_mark = True
            elif can_attach_mark and unicodedata.category(character).startswith("M"):
                characters.append(character)
            else:
                can_attach_mark = False
        tag = "".join(characters)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(f"#{tag}")
        if count and len(result) >= count:
            break
    return result


def build_social_metadata_prompt(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> str:
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    platform = _resolve_social_platform(platform)
    spec = SOCIAL_PLATFORMS[platform]
    label = SOCIAL_PLATFORM_LABELS.get(platform, platform)
    language_instruction = _social_language_instruction(language)

    prompt = f"""
# Role: Short-Video Social Media Copywriter

## Goal
Write engaging publishing metadata for a short video that will be posted on {label}.

## Constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. The JSON must contain exactly these keys: "title", "caption", "hashtags".
3. "title": a catchy hook, at most {spec["title_max"]} characters.
4. "caption": an engaging description that ends with a call to action, at most {spec["caption_max"]} characters. Do not put hashtags inside the caption.
5. "hashtags": a JSON array of exactly {spec["hashtag_count"]} strings. Each must start with "#", contain no spaces, and be relevant to the topic and to {label}.
6. {language_instruction}

## Output Example
{{"title":"...","caption":"...","hashtags":["#example","#video"]}}

## Context
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()
    return prompt


def _parse_social_metadata(response: str, platform: str) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]

    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception:
        # 部分模型会在 JSON 外层包一段说明文字或 markdown fence。
        # API 调用方只需要稳定结构，所以这里尝试提取第一个 JSON object。
        data = _extract_embedded_json(response, "{")

    if not isinstance(data, dict):
        raise ValueError("social metadata response is not a JSON object")

    title = _clamp_text(data.get("title", ""), spec["title_max"])
    caption = _clamp_text(data.get("caption", ""), spec["caption_max"])
    hashtags = _normalize_hashtags(data.get("hashtags", []), spec["hashtag_count"])

    if not title and not caption:
        raise ValueError("social metadata response is missing both title and caption")

    return {"title": title, "caption": caption, "hashtags": hashtags}


def _fallback_social_metadata(
    video_subject: str, video_script: str, platform: str
) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]
    subject = (video_subject or "").strip()
    script = (video_script or "").strip()

    title = subject
    if not title and script:
        # 中日文句末通常没有空格；英文仍要求空白，避免拆开 2.0 等数字。
        title = re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*", script, maxsplit=1)[0]

    return {
        "title": _clamp_text(title, spec["title_max"]),
        "caption": _clamp_text(script or subject, spec["caption_max"]),
        "hashtags": _normalize_hashtags(DEFAULT_SOCIAL_HASHTAGS, spec["hashtag_count"]),
    }


def generate_social_metadata(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> dict:
    """
    生成短视频发布文案元数据。

    返回结构固定为 `{"title": str, "caption": str, "hashtags": List[str]}`。
    如果 LLM 不可用或返回格式异常，会降级为通用启发式结果，保证 API
    调用方始终拿到可展示、可发布前编辑的数据结构。
    """
    platform = _resolve_social_platform(platform)
    language = _normalize_social_language(language)
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    prompt = build_social_metadata_prompt(
        video_subject=video_subject,
        video_script=video_script,
        language=language,
        platform=platform,
    )
    logger.info(f"generating social metadata: platform={platform}, language={language}")

    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and response.startswith("Error: "):
                logger.error(f"failed to generate social metadata: {response}")
                break
            metadata = _parse_social_metadata(response, platform)
            logger.success(f"completed: \n{metadata}")
            return metadata
        except Exception as e:
            logger.warning(f"failed to parse social metadata: {str(e)}")

        if i < _max_retries - 1:
            logger.warning(
                f"failed to generate social metadata, trying again... {i + 1}"
            )

    logger.warning("falling back to heuristic social metadata")
    return _fallback_social_metadata(video_subject, video_script, platform)
# =============================================================================
# YouTube publishing metadata
#
# 发到 YouTube 时要填的那三样：标题、简介、封面。前两样是文字，第三样是一张
# 图，但三样都取决于同一件事——这条片子是竖的还是横的。
#
# Why the aspect ratio is an input and not a detail
# -------------------------------------------------
# 9:16 的片子进的是 Shorts：标题要短到在手机上一眼看完，简介要带 #Shorts，
# 而封面是一张**竖**图，上下各一截会被播放器自己的按钮盖掉。16:9 的片子进的
# 是普通视频页：封面是一张**横**图，一侧可以留白给大字。
#
# 把这两种当成同一件事，结果是一张横图配一条竖片——传上去 YouTube 会把它
# 两边裁掉，主体正好被裁没。所以画幅在这里是必填的输入，不是一个细节。
#
# The aspect ratio decides what to write and what shape the thumbnail is, so
# it is an input here rather than an afterthought: a landscape thumbnail on a
# vertical Short is cropped on both sides, which is exactly where the subject
# usually is.
# =============================================================================

#: 每种画幅对应的 YouTube 形态。
YOUTUBE_FORMATS = {
    "9:16": {
        "kind": "short",
        "label": "YouTube Shorts (a vertical video watched on a phone)",
        "title_max": 100,
        "description_max": 5000,
        "tag_count": 10,
        "thumbnail_label": "a tall 9:16 portrait image",
        "framing": (
            "The thumbnail frame is TALL and narrow. Keep the subject in the "
            "middle third and large enough to read on a phone. Leave the top "
            "eighth and the bottom quarter free of anything important: the "
            "Shorts player covers them with the title, the channel name and "
            "its own buttons."
        ),
        "advice": (
            "Shorts are found by scrolling, not by searching, so the title has "
            "to land in its first few words. Put #Shorts in the description."
        ),
    },
    "16:9": {
        "kind": "standard",
        "label": "a standard widescreen YouTube video",
        "title_max": 100,
        "description_max": 5000,
        "tag_count": 12,
        "thumbnail_label": "a wide 16:9 landscape image",
        "framing": (
            "The thumbnail frame is WIDE. Put the subject on one side and "
            "leave the other side comparatively plain, so that a few large "
            "words could be laid over it. Keep everything important away from "
            "the bottom right corner, where the duration badge sits."
        ),
        "advice": (
            "A standard video is found by searching, so the title should "
            "contain the words someone would actually type, and the first two "
            "lines of the description are all that shows before 'Show more'."
        ),
    },
    "1:1": {
        "kind": "square",
        "label": "a square YouTube video",
        "title_max": 100,
        "description_max": 5000,
        "tag_count": 12,
        "thumbnail_label": "a square 1:1 image",
        "framing": (
            "The thumbnail frame is SQUARE. Centre the subject and keep the "
            "margins clear, because a square frame is cropped differently in "
            "the feed and on the watch page."
        ),
        "advice": (
            "Write a title that reads well both as a search result and in a "
            "feed."
        ),
    },
}
DEFAULT_YOUTUBE_ASPECT = "9:16"
MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH = 600


def youtube_format(aspect=None) -> dict:
    """这条片子对应的 YouTube 形态。认不出画幅时按竖版算。

    竖版是这个项目的默认画幅，所以猜错的代价最小；而且它的构图约束比横版
    更紧（上下要给播放器让位），按它生成的封面放进横版只是保守了一点，反
    过来则会被裁掉主体。
    """
    key = str(aspect or "").strip() or DEFAULT_YOUTUBE_ASPECT
    return YOUTUBE_FORMATS.get(key, YOUTUBE_FORMATS[DEFAULT_YOUTUBE_ASPECT])


def _normalize_youtube_tags(raw, count: int) -> List[str]:
    """YouTube 的标签是普通词组，不是 hashtag——所以这里不加 ``#``。

    不复用 ``_normalize_hashtags``：它会把 "street food" 挤成 "#streetfood"，
    而 YouTube 的标签框本来就接受带空格的词组，挤掉空格只会让它们更难被
    搜到。
    """
    if isinstance(raw, str):
        candidates = re.split(r"[,，、;；\n]+", raw)
    elif isinstance(raw, (list, tuple)):
        candidates = [str(entry) for entry in raw]
    else:
        candidates = []

    seen = set()
    tags: List[str] = []
    for item in candidates:
        tag = re.sub(r"\s+", " ", str(item).replace("#", "")).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if count and len(tags) >= count:
            break
    return tags


def build_youtube_metadata_prompt(
    video_subject: str,
    video_script: str = "",
    aspect: str = DEFAULT_YOUTUBE_ASPECT,
    language: str = DEFAULT_SOCIAL_LANGUAGE,
) -> str:
    """要模型一次写出标题、简介、标签和封面的画面描述。

    封面描述也交给它，而不是把主题原样丢给文生图：封面要画的是"这条片子在
    讲什么"的一个画面，而主题常常是一句抽象的话。顺带，画幅的构图约束写进
    提示词，模型才有机会把主体放在不会被播放器盖住的位置。
    """
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    spec = youtube_format(aspect)
    language_instruction = _social_language_instruction(language)

    return f"""
# Role: YouTube Packaging Specialist

## Goal
Write the title, the description, the tags and a thumbnail brief for {spec["label"]}.

## What you know about the video
The finished video is {aspect}. {spec["advice"]}

## Constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. The JSON must contain exactly these keys: "title", "description", "tags", "thumbnail_prompt".
3. "title": at most {spec["title_max"]} characters. No promise the script does not keep.
4. "description": at most {spec["description_max"]} characters. Open with two lines that stand on their own, then the detail, then a call to action. Invent no links, no timestamps and no sponsors.
5. "tags": a JSON array of up to {spec["tag_count"]} plain search phrases. No "#"; spaces are allowed.
6. "thumbnail_prompt": one English paragraph of at most {MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH} characters describing a single image for a text-to-image model: the subject, the setting, the lighting and the mood. {spec["framing"]} Ask for no words, letters or logos anywhere in the image.
7. {language_instruction} Keep "thumbnail_prompt" in English, because the image model is the one reading it.

## Output Example
{{"title":"...","description":"...","tags":["example tag"],"thumbnail_prompt":"..."}}

## Context
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()


def _parse_youtube_metadata(response: str, aspect: str) -> dict:
    spec = youtube_format(aspect)
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception:
        # 有的模型会在 JSON 外面裹一段说明文字或 markdown fence。
        data = _extract_embedded_json(response, "{")

    if not isinstance(data, dict):
        raise ValueError("youtube metadata response is not a JSON object")

    title = _clamp_text(data.get("title", ""), spec["title_max"])
    description = _clamp_text(data.get("description", ""), spec["description_max"])
    tags = _normalize_youtube_tags(data.get("tags", []), spec["tag_count"])
    thumbnail_prompt = _clamp_text(
        data.get("thumbnail_prompt", ""), MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH
    )

    if not title:
        raise ValueError("youtube metadata response is missing the title")

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "thumbnail_prompt": thumbnail_prompt,
    }


def _fallback_youtube_metadata(
    video_subject: str, video_script: str, aspect: str
) -> dict:
    """模型不可用时也要给出一份能改的草稿，而不是一个空面板。

    标题取主题（或脚本第一句），简介取脚本本身，封面描述按画幅拼一句。都
    不算好，但都是真话，而且用户可以在界面里改。返回空结构的话，用户只能
    对着一个什么都没有的面板，分不清是模型坏了还是这个功能坏了。
    """
    spec = youtube_format(aspect)
    subject = (video_subject or "").strip()
    script = (video_script or "").strip()

    title = subject
    if not title and script:
        # 中日文句末通常没有空格；英文仍要求空白，避免拆开 2.0 这类数字。
        title = re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*", script, maxsplit=1)[0]

    description = script or subject
    if spec["kind"] == "short" and description:
        description = f"{description}\n\n#Shorts"

    topic = subject or title or "the subject of this video"
    return {
        # 这一份不是模型写的。界面必须能说出这件事：标题被拆成几个词、标签
        # 是主题的分词，用户看到的是一份明显很差的文案，而"模型压根没跑"和
        # "模型就这水平"是完全不同的两件事——前者改配置就好了。
        "source": "fallback",
        "title": _clamp_text(title, spec["title_max"]),
        "description": _clamp_text(description, spec["description_max"]),
        "tags": _normalize_youtube_tags(
            [word for word in re.split(r"[\s,，、]+", topic) if len(word) > 2],
            spec["tag_count"],
        ),
        "thumbnail_prompt": _clamp_text(
            f"A clear, striking photograph of {topic}, dramatic lighting, "
            f"no text or letters anywhere in the image. {spec['framing']}",
            MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH,
        ),
    }


def generate_youtube_metadata(
    video_subject: str,
    video_script: str = "",
    aspect: str = DEFAULT_YOUTUBE_ASPECT,
    language: str = DEFAULT_SOCIAL_LANGUAGE,
) -> dict:
    """生成 YouTube 的标题、简介、标签和封面提示词。

    返回结构固定为 ``{"title": str, "description": str, "tags": List[str],
    "thumbnail_prompt": str}``。模型不可用或返回格式异常时降级为启发式结果，
    保证调用方总能拿到一份可展示、可编辑的数据。
    """
    language = _normalize_social_language(language)
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    prompt = build_youtube_metadata_prompt(
        video_subject=video_subject,
        video_script=video_script,
        aspect=aspect,
        language=language,
    )
    logger.info(f"generating youtube metadata: aspect={aspect}, language={language}")

    reason = ""
    for attempt in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and response.startswith("Error: "):
                logger.error(f"failed to generate youtube metadata: {response}")
                # 这条就是要显示给用户的那一句：它点名了是哪个 provider 的
                # 哪一项没配好。
                reason = response[len("Error: "):]
                break
            metadata = _parse_youtube_metadata(response, aspect)
            metadata["source"] = "model"
            logger.success(f"completed: \n{metadata}")
            return metadata
        except Exception as e:
            reason = str(e)
            logger.warning(f"failed to parse youtube metadata: {reason}")
        if attempt < _max_retries - 1:
            logger.warning(
                f"failed to generate youtube metadata, trying again... {attempt + 1}"
            )

    logger.warning("falling back to heuristic youtube metadata")
    fallback = _fallback_youtube_metadata(video_subject, video_script, aspect)
    # 为什么降级，一并带上。"ollama: model_name is not set" 是用户改一行配置
    # 就能解决的事，而一份没署名的烂文案只会让人以为这个功能不行。
    fallback["error"] = reason
    return fallback


if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)
