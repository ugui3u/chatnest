"""Claude Agent SDK wrapper with streaming, thinking, and session resume."""

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    get_session_info,
)
from claude_agent_sdk.types import StreamEvent

from app.actor import ActorBusyError
from app.memory import build_profile_context, memory_tool_permission, read_memory
from app.registry import get_registry


_haiku_sem = asyncio.Semaphore(2)

MEMORY_SEARCH_URL = os.environ.get("MEMORY_SEARCH_URL", "http://127.0.0.1:3900/search")
MEMORY_SEARCH_TOP_K = 6
MEMORY_SEARCH_BUDGET_CHARS = 1500
MEMORY_SEARCH_TIMEOUT_S = 2.0
memlog = logging.getLogger("memory.inject")


async def fetch_memory_hits(query: str) -> str:
    """POST to local hybrid-search service. Silent-fail on any error."""
    if not query.strip():
        return ""

    def _call() -> dict | None:
        body = json.dumps(
            {
                "query": query,
                "top_k": MEMORY_SEARCH_TOP_K,
                "budget_chars": MEMORY_SEARCH_BUDGET_CHARS,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            MEMORY_SEARCH_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=MEMORY_SEARCH_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            memlog.info("memory search unavailable: %s", e)
            return None

    data = await asyncio.to_thread(_call)
    if not data:
        return ""
    joined = (data.get("joined") or "").strip()
    stats = data.get("stats") or {}
    memlog.info(
        "memory search ok: %d chunks, vec=%d bm25=%d, %sms, %d chars injected",
        len(data.get("results") or []),
        stats.get("vec_hits", 0),
        stats.get("bm25_hits", 0),
        stats.get("ms", "?"),
        len(joined),
    )
    return joined


SYSTEM_PROMPT = """\
You are a warm, concise assistant in a personal chat app. Reply naturally, respect the user's saved profile and preferences, and use tools only when they help. When you save long-term memories, save objective user facts rather than conversation summaries.
"""
PROJECT_ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
MODELS_PATH = Path(os.environ.get("MODELS_FILE", PROJECT_ROOT / "models.json")).expanduser().resolve()
PROJECT_DIR = str(PROJECT_ROOT)
SUMMARY_PROMPT = (
    "你是一个摘要工具。你将收到一段AI的内心思考过程，你的唯一任务是输出一句不超过20字的中文概括。"
    "要求：动词短语开头，写出决策或权衡，不要复述内容，不要加’思考’/’分析’等元描述词。"
    "禁止：不要回复对话，不要加emoji，不要说’我理解’/’让我’/’好的’，不要输出任何非摘要内容。"
    "风格参考（动词要多样化，禁止反复使用同一句式）：’决定静默陪伴而非催促’、’梳理求职时间线’、"
    "’用日常语气接住情绪’、’定位z-index层级冲突’、’回忆上次聊过的话题’、"
    "’组织多条建议的优先级’、’拆解前端布局问题’、’斟酌措辞避免说教感’、"
    "’对比两种技术方案利弊’、’补充遗漏的边界情况’、’绕开敏感话题切入正题’、"
    "’核实时间日期再作答’、’顺着她的情绪往下聊’、’挑选最贴切的类比解释’。"
    "只输出摘要本身，不要有任何其他文字。"
)


class SessionResumeError(RuntimeError):
    pass


def available_models() -> list[dict]:
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    return [
        {
            "id": str(item["id"]),
            "label": str(item["label"]),
            "desc": str(item["desc"]),
            "thinking": str(item["thinking"]),
            "primary": bool(item["primary"]),
        }
        for item in models
    ]


def thinking_options(
    model: dict,
    effort: str,
    extended: bool,
) -> tuple[dict, str | None]:
    if model["thinking"] == "none":
        return {"type": "disabled"}, None
    allowed_efforts = {"low", "medium", "high", "max"}
    selected = effort if effort in allowed_efforts else "medium"
    if model["thinking"] == "adaptive":
        return {"type": "adaptive"}, selected
    if extended:
        return {"type": "enabled", "budget_tokens": 8_000}, selected
    return {"type": "disabled"}, selected


async def build_system_prompt(message: str, model: str) -> str:
    """Stable profile context shared by SDK and API backends.

    Per-turn recall is added once to the user message by app.main. Keep this
    async two-argument interface compatible with both callers.
    """
    profile_context = build_profile_context().strip()
    memory = "" if profile_context else read_memory().strip()
    system_prompt = f"You are running as model {model}. If asked which model you are, answer with that identifier.\n\n{SYSTEM_PROMPT}"
    if profile_context:
        system_prompt += (
            "\n\n以下是用户在 Profile 中保存的资料、长期记忆和模型偏好。"
            "Saved memories 是事实记忆；Preferences 是用户明确要求的回复偏好，"
            "应在不违反系统要求时遵守：\n"
            f"{profile_context}"
        )
    if memory:
        system_prompt += f"\n\n以下是用户明确保存的长期记忆：\n{memory}"
    return system_prompt


async def stream_chat(
    message: str,
    conv_id: str,
    session_id: str | None = None,
    model: str = "claude-sonnet-4-6",
    effort: str = "medium",
    extended: bool = True,
    timing_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[dict, None]:
    model_config = next(
        (item for item in available_models() if item["id"] == model),
        None,
    )
    if model_config is None:
        raise ValueError("unsupported model")
    if session_id and get_session_info(session_id, directory=PROJECT_DIR) is None:
        raise SessionResumeError("会话恢复失败")
    thinking, selected_effort = thinking_options(model_config, effort, extended)

    system_prompt = await build_system_prompt(message, model)

    option_values = dict(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=["Read", "Grep", "Glob", "Write", "Edit", "Bash", "WebSearch", "WebFetch", "TodoWrite"],
        can_use_tool=memory_tool_permission,
        max_turns=8,
        include_partial_messages=True,
        thinking=thinking,
        resume=session_id,
        setting_sources=[],
        cwd=PROJECT_DIR,
    )
    if selected_effort is not None:
        option_values["effort"] = selected_effort
    options = ClaudeAgentOptions(**option_values)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "thinking": thinking,
                "effort": selected_effort,
                "system_prompt": system_prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    outbox = await get_registry().submit(
        conv_id,
        message,
        options,
        fingerprint,
        timing_callback,
    )
    while True:
        item = await outbox.get()
        if item is None:
            break
        if isinstance(item, Exception):
            if isinstance(item, ActorBusyError):
                raise item
            if "resume" in str(item).lower():
                raise SessionResumeError("会话恢复失败") from item
            raise item
        yield item


async def summarize_thinking(thinking: str) -> str:
    async with _haiku_sem:
        logger = logging.getLogger(__name__)
        options = ClaudeAgentOptions(
            model="claude-haiku-4-5",
            system_prompt=SUMMARY_PROMPT,
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.01,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(thinking[:8000])
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        summary = text.strip().strip('"""\'')
        logger.info("thinking_summary raw=%r truncated=%r", text[:100], summary[:40])
        if not summary or "not logged in" in summary.lower():
            raise RuntimeError("thinking summary unavailable")
        return summary[:40]


TRACE_SUMMARY_PROMPT = (
    "你是一个摘要工具。你的唯一任务是输出一句不超过15字的中文概括。"
    "动词短语开头，写出目的而非动作本身，不要引号，不要出现’调用’/’执行’。"
    "禁止：不要回复对话，不要加emoji，不要说’我理解’/’让我’/’好的’，不要输出任何非摘要内容。"
    "风格参考：’排查侧边栏渲染异常’、’验证数据库连接配置’。只输出摘要本身。"
)


async def summarize_traces(traces: list[dict]) -> str:
    tool_results = {
        t.get("tool_use_id"): t
        for t in traces
        if t.get("type") == "tool_result"
    }
    parts = []
    for t in traces:
        if t.get("type") != "tool_use":
            continue
        result = tool_results.get(t.get("id"), {})
        try:
            input_str = (
                t.get("input", "")
                if isinstance(t.get("input"), str)
                else json.dumps(t.get("input", {}), ensure_ascii=False)
            )
        except Exception:
            input_str = str(t.get("input", ""))
        output_str = (result.get("content") or "")[:300]
        parts.append(
            f"工具: {t.get('name', 'tool')}\n"
            f"输入: {input_str[:200]}\n"
            f"输出: {output_str}"
        )
    if not parts:
        return ""
    prompt = "\n---\n".join(parts)
    async with _haiku_sem:
        options = ClaudeAgentOptions(
            model="claude-haiku-4-5",
            system_prompt=TRACE_SUMMARY_PROMPT,
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.01,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(prompt)
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        summary = text.strip()
        for ch in ['"', "'", '"', '"', '。', '.', '，', ',']:
            summary = summary.strip(ch)
        return summary[:30] if summary else ""


async def summarize_tool_use(tool_name: str, tool_input, tool_output: str) -> str:
    try:
        input_str = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        input_str = str(tool_input or "")
    output_snip = (tool_output or "")[:600]
    prompt = "工具名：" + tool_name + "\n输入：" + input_str[:400] + "\n输出片段：" + output_snip
    async with _haiku_sem:
        options = ClaudeAgentOptions(
            model="claude-haiku-4-5",
            system_prompt=(
                "你是一个摘要工具。你的唯一任务是输出一句不超过15字的中文概括。"
                "动词短语开头，写出目的而非动作本身，不要引号，不要描述结果，"
                "不要出现’调用’/’执行’。禁止回复对话、加emoji、说’我理解’/’让我’/’好的’。"
                "风格参考：’排查配置文件格式问题’、’确认端口占用情况’。只输出摘要本身。"
            ),
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.005,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(prompt)
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        caption = text.strip()
        for ch in ['"', "'", '"', '"', '。', '.', '，', ',']:
            caption = caption.strip(ch)
    return caption[:20] if caption else ""
