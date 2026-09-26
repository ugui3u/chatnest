"""Direct Anthropic-compatible Messages API (no Claude Code login required)."""

import base64
import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import anthropic

from app.claude import available_models, build_system_prompt, SUMMARY_PROMPT
from app.store import conversation_messages
from app.uploads import UPLOAD_ROOT


def validate_api_settings() -> tuple[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ValueError("API 模式需要在 .env 中设置 ANTHROPIC_API_KEY，并重启服务")
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("ANTHROPIC_BASE_URL 必须是完整的 http(s) 服务地址，不能含凭据或查询参数")
    if parsed.path.endswith(("/messages", "/chat/completions", "/responses")):
        raise ValueError("请填写 API 基础地址，不要填写 /v1/messages 等完整请求路径")
    # The SDK appends /v1/messages. Accept the common base URL ending in /v1.
    return key, base.removesuffix("/v1")


def _get_client() -> anthropic.AsyncAnthropic:
    key, base = validate_api_settings()
    return anthropic.AsyncAnthropic(api_key=key, base_url=base)


def _attachment_blocks(items: list[dict]) -> list[dict]:
    blocks = []
    for item in items:
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(UPLOAD_ROOT.resolve()) or not path.is_file():
            raise ValueError("API 附件不存在或路径无效，请重新上传")
        name = item.get("name") or path.name
        mime = item.get("mime", "")
        if mime in {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError(f"API 模式单个图片或 PDF 限制为 8MB：{name}")
            blocks.append({
                "type": "document" if mime == "application/pdf" else "image",
                "source": {"type": "base64", "media_type": mime,
                           "data": base64.b64encode(path.read_bytes()).decode("ascii")},
            })
        elif item.get("is_image"):
            raise ValueError(f"API 模式请将图片转换为 PNG/JPEG/GIF/WebP：{name}")
        else:
            if path.stat().st_size > 200_000:
                raise ValueError(f"API 模式文本附件限制为 200KB：{name}")
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"API 模式文本附件需要 UTF-8 编码：{name}") from exc
            blocks.append({"type": "text", "text": f"[附件：{name}]\n{content}"})
    return blocks


def _build_history(conv_id: str, message: str, user_message_id: int) -> list[dict]:
    rows, _, _ = conversation_messages(conv_id)
    messages = []
    found_current = False
    for row in rows:
        # Only replay visible text: stored thinking does not retain signatures.
        text = row.get("text") or ""
        if row["id"] == user_message_id:
            text = message  # Replace the persisted user text with enriched context, once.
            found_current = True
        content = [{"type": "text", "text": text}] if text else []
        if row["role"] == "user":
            content.extend(_attachment_blocks(row.get("attachments") or []))
        if content:
            messages.append({"role": row["role"], "content": content})
        if found_current:
            break
    if not found_current:
        raise ValueError("当前消息不在会话历史中，请刷新后重试")
    return messages


async def stream_chat_api(
    message: str,
    conv_id: str,
    model: str = "claude-sonnet-4-6",
    effort: str = "medium",
    extended: bool = True,
    timing_callback: Callable[[str], None] | None = None,
    *,
    user_message_id: int,
) -> AsyncGenerator[dict, None]:
    model_config = next((m for m in available_models() if m["id"] == model), None)
    if model_config is None:
        raise ValueError("unsupported model")
    system_prompt = await build_system_prompt(message, model)
    system_prompt += "\n当前使用直接 API 聊天模式，没有可执行工具。不要声称已搜索网页、执行命令或保存文件。"
    kwargs = {
        "model": model,
        "system": system_prompt,
        "messages": _build_history(conv_id, message, user_message_id),
        "max_tokens": 16384,
    }
    if extended and model_config["thinking"] == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort if effort in {"low", "medium", "high", "max"} else "medium"}
    elif extended and model_config["thinking"] == "extended":
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    client = _get_client()
    first_text = False
    try:
        async with client.messages.stream(**kwargs) as stream:
            if timing_callback:
                timing_callback("api_first_event")
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "thinking_delta":
                        yield {"event": "thinking", "text": delta.thinking}
                    elif delta.type == "text_delta":
                        if not first_text:
                            first_text = True
                            if timing_callback:
                                timing_callback("first_text_token")
                        yield {"event": "delta", "text": delta.text}
        yield {"event": "done", "session_id": f"api-{uuid4().hex[:12]}"}
    finally:
        await client.close()


async def summarize_api(text: str) -> str:
    """Optional API-only captions; never fall back to a CC login."""
    model = os.environ.get("API_SUMMARY_MODEL", "").strip()
    if not model or not text.strip():
        return ""
    client = _get_client()
    try:
        result = await client.messages.create(
            model=model, max_tokens=160, system=SUMMARY_PROMPT,
            messages=[{"role": "user", "content": text[:8000]}],
        )
        return "".join(getattr(block, "text", "") for block in result.content).strip()[:40]
    finally:
        await client.close()
