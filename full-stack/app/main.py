import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import anthropic
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any
from starlette.formparsers import MultiPartParser

MultiPartParser.max_part_size = 60 * 1024 * 1024  # 与 uploads.py 的 MAX_FILE_BYTES 对齐

from app import auth
from app.actor import ActorBusyError
from app.claude import (
    SessionResumeError,
    available_models,
    stream_chat,
    summarize_thinking,
    summarize_tool_use,
    summarize_traces,
)
from app.claude_api import stream_chat_api, summarize_api, validate_api_settings
from app.codex_api import stream_codex_chat
from app.memory import (
    MAX_MEMORY_CHARS,
    add_saved_memory,
    import_claude_export_memories,
    read_diary_entries,
    read_memory,
    read_profile,
    write_memory,
    write_profile,
)
from app.memory_search import recall as recall_memory
from app.sessions import (
    remove_session,
    session_list,
    session_messages,
    set_session_starred,
    set_session_title,
)
from app.splash import current_period, random_line
from app.registry import configure_registry, get_registry
from app.store import (
    ConversationNotFound,
    begin_turn,
    complete_turn,
    conversation_messages,
    ensure_conversation,
    initialize_store,
    prepare_edit_turn,
    prepare_retry_turn,
    resolve_conversation,
    restore_branch,
)
from app.uploads import (
    remove_conversation_uploads,
    save_uploads,
    validated_attachments,
    validated_file,
)


logger = logging.getLogger(__name__)
timing_logger = logging.getLogger("uvicorn.error")
STATIC = ROOT / "static"
chat_lock = asyncio.Lock()
initialize_store()
TRACE_CONTENT_CHARS = 20_000


def trace_content(value: Any) -> str:
    text = str(value or "")
    if len(text) <= TRACE_CONTENT_CHARS:
        return text
    return text[:TRACE_CONTENT_CHARS] + "\n\n[output truncated]"


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = configure_registry(str(ROOT))
    await registry.start()
    try:
        yield
    finally:
        await registry.stop()


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

CHAT_BACKEND = os.environ.get("CHAT_BACKEND", "sdk").strip().lower()
if CHAT_BACKEND not in {"sdk", "api"}:
    raise ValueError("CHAT_BACKEND must be sdk or api")

AUTH_MODE = os.environ.get("AUTH_MODE", "app").strip().lower()
if AUTH_MODE not in {"app", "both"}:
    logger.warning("Unsupported AUTH_MODE=%r; falling back to app", AUTH_MODE)
    AUTH_MODE = "app"
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
OUTER_AUTH_COOKIE = "claude_outer_auth"
OUTER_BASIC_AUTH_ENABLED = AUTH_MODE == "both" and bool(BASIC_AUTH_USER and BASIC_AUTH_PASSWORD)


def outer_auth_token() -> str:
    return hmac.new(
        os.environ["CHAT_SECRET"].encode(),
        f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}:outer-v1".encode(),
        "sha256",
    ).hexdigest()


def basic_auth_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD)


def request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


@app.middleware("http")
async def outer_basic_auth(request: Request, call_next):
    public_paths = (
        "/health",
        "/marked.min.js",
        "/favicon.ico",
        "/static/manifest.webmanifest",
        "/static/css/typography-locked.css",
        "/static/design-system.css",
    )
    if not OUTER_BASIC_AUTH_ENABLED or request.url.path in public_paths:
        return await call_next(request)
    token = request.cookies.get(OUTER_AUTH_COOKIE, "")
    if token and hmac.compare_digest(token, outer_auth_token()):
        return await call_next(request)
    if not basic_auth_ok(request.headers.get("authorization", "")):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Agent App"'},
        )
    response = await call_next(request)
    response.set_cookie(
        OUTER_AUTH_COOKIE,
        outer_auth_token(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=request_is_https(request),
        samesite="lax",
    )
    return response


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


class AuthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    edit_message_id: int | None = Field(default=None, ge=1)
    retry_message_id: int | None = Field(default=None, ge=1)
    model: str = Field(default="claude-sonnet-4-6", max_length=64)
    effort: str = Field(default="medium", max_length=16)
    extended: bool = True
    attachments: list[str] = Field(default_factory=list, max_length=10)


class ToolCaptionBody(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    tool_input: Any = None
    tool_output: str = Field(default="", max_length=20000)


class MemoryBody(BaseModel):
    content: str = Field(max_length=MAX_MEMORY_CHARS)


class ProfileBody(BaseModel):
    fullName: str = Field(default="", max_length=200)
    nickname: str = Field(default="", max_length=200)
    savedMemories: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    preferences: dict[str, Any] = Field(default_factory=dict)
    claudeExportImport: dict[str, Any] = Field(default_factory=dict)
    updatedAt: int | None = None


class ThinkingSummaryBody(BaseModel):
    thinking: str = Field(min_length=1, max_length=50_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class StarBody(BaseModel):
    starred: bool


def render_context_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    if len(messages) == 1 and last["role"] == "user":
        prompt = last["text"]
    else:
        lines = []
        for message in messages:
            role = "User" if message["role"] == "user" else "Assistant"
            text = (message.get("text") or "").strip()
            attachments = message.get("attachments") or []
            if attachments:
                paths = "\n".join(
                    item.get("path", "")
                    for item in attachments
                    if item.get("path")
                )
                if paths:
                    text = f"{text}\n[attachments]\n{paths}".strip()
            lines.append(f"{role}: {text}")
        prompt = (
            "<conversation-context>\n"
            + "\n\n".join(lines[:-1])
            + "\n</conversation-context>\n\n"
            "Please continue from the context above and answer only the final "
            "user message below. Do not repeat the previous transcript.\n\n"
            f"User: {last.get('text', '').strip()}"
        )
    attachments = last.get("attachments") or []
    paths = "\n".join(
        item.get("path", "")
        for item in attachments
        if item.get("path")
    )
    if paths:
        prompt += (
            "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
            f"{paths}\n]"
        )
    return prompt


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/static/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    return Response(status_code=204)


@app.get("/static/css/typography-locked.css", include_in_schema=False)
async def typography_locked() -> Response:
    return Response(status_code=204)


@app.get("/marked.min.js")
async def marked_js() -> FileResponse:
    return FileResponse(
        STATIC / "marked.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/static/design-system.css")
async def design_system_css() -> FileResponse:
    return FileResponse(
        STATIC / "design-system.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/auth")
async def login(body: AuthBody) -> dict:
    token = auth.issue_token(body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"token": token}


@app.post("/api/chat", dependencies=[Depends(require_auth)])
async def chat(body: ChatBody) -> StreamingResponse:
    request_id = uuid4().hex[:12]
    request_started = perf_counter()
    timing_stages = {"request_received"}

    def log_timing(stage: str) -> None:
        if stage in timing_stages:
            return
        timing_stages.add(stage)
        timing_logger.info(
            "chat_timing request_id=%s stage=%s at_utc=%s elapsed_ms=%.1f",
            request_id,
            stage,
            datetime.now(UTC).isoformat(timespec="milliseconds"),
            (perf_counter() - request_started) * 1000,
        )

    timing_logger.info(
        "chat_timing request_id=%s stage=request_received at_utc=%s "
        "elapsed_ms=0.0",
        request_id,
        datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    is_branch_turn = body.edit_message_id is not None or body.retry_message_id is not None
    if body.edit_message_id is not None and body.retry_message_id is not None:
        raise HTTPException(status_code=400, detail="一次只能执行一种分支操作")
    if not is_branch_turn and not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="消息或附件不能为空")
    requested_conv_id = body.conversation_id or body.session_id
    if is_branch_turn and not requested_conv_id:
        raise HTTPException(status_code=400, detail="分支操作缺少会话标识")
    if body.attachments and not requested_conv_id:
        raise HTTPException(status_code=400, detail="附件缺少会话标识")
    attachment_items = (
        validated_attachments(requested_conv_id, body.attachments)
        if body.attachments and requested_conv_id
        else []
    )

    async def sse():
        if chat_lock.locked():
            payload = json.dumps(
                {"message": "上一条消息仍在回复"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
            return
        await chat_lock.acquire()
        conv_id = None
        user_message_id = None
        branch_restore_id = None
        branch_committed = False
        response_text = ""
        response_thinking = ""
        response_traces: list[dict] = []
        pending_chunk = None
        chunk_iter = None
        use_api = CHAT_BACKEND == "api" and body.model != "codex"
        try:
            if use_api:
                validate_api_settings()
            await get_registry().assert_available()
            display_message = body.message.strip()
            current_attachment_items = attachment_items
            context_messages = None
            if body.edit_message_id is not None:
                prepared = prepare_edit_turn(
                    requested_conv_id or "",
                    body.edit_message_id,
                    display_message,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            elif body.retry_message_id is not None:
                prepared = prepare_retry_turn(
                    requested_conv_id or "",
                    body.retry_message_id,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            else:
                conv_id, resume_id, user_message_id = begin_turn(
                    display_message,
                    body.conversation_id,
                    body.session_id,
                    current_attachment_items,
                )
            payload = json.dumps(
                {
                    "conversation_id": conv_id,
                    "user_message_id": user_message_id,
                },
                ensure_ascii=False,
            )
            yield f"event: conversation\ndata: {payload}\n\n"
            if not use_api and body.model != "codex" and resume_id and resume_id.startswith("api-"):
                context_messages, _, _ = conversation_messages(conv_id)
                resume_id = None
            prompt = (
                render_context_prompt(context_messages)
                if context_messages and not use_api
                else display_message
            )
            recalled = recall_memory(display_message)
            if recalled:
                prompt = (
                    "<recalled-memory>\n"
                    "以下是从家用记忆里检索到的相关片段，按相关度排序。"
                    "可能与这次请求相关，参考着用；不相关就忽略。\n\n"
                    f"{recalled}\n"
                    "</recalled-memory>\n\n"
                    f"{prompt}"
                )
            if current_attachment_items and not context_messages and not use_api:
                paths = "\n".join(item["path"] for item in current_attachment_items)
                prompt += (
                    "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
                    f"{paths}\n]"
                )
            chat_args = (prompt, conv_id, resume_id, body.model,
                         body.effort, body.extended, log_timing)
            if body.model == "codex":
                chat_stream = stream_codex_chat(*chat_args)
                first_chunk = await chat_stream.__anext__()
            elif use_api:
                chat_stream = stream_chat_api(
                    prompt, conv_id, body.model, body.effort, body.extended,
                    log_timing, user_message_id=user_message_id,
                )
                first_chunk = None
            else:
                try:
                    chat_stream = stream_chat(*chat_args)
                    first_chunk = await chat_stream.__anext__()
                except (SessionResumeError, StopAsyncIteration):
                    logger.info("session resume failed for conv=%s, retrying without session", conv_id)
                    chat_args = (prompt, conv_id, None, body.model,
                                 body.effort, body.extended, log_timing)
                    chat_stream = stream_chat(*chat_args)
                    first_chunk = await chat_stream.__anext__()

            async def _merged():
                try:
                    if first_chunk is not None:
                        yield first_chunk
                    async for c in chat_stream:
                        yield c
                finally:
                    await chat_stream.aclose()

            heartbeat_interval = 15
            chunk_iter = _merged().__aiter__()
            while True:
                try:
                    if pending_chunk is None:
                        pending_chunk = asyncio.create_task(chunk_iter.__anext__())
                    done, _ = await asyncio.wait({pending_chunk}, timeout=heartbeat_interval)
                    if not done:
                        yield ": heartbeat\n\n"
                        continue
                    chunk = pending_chunk.result()
                    pending_chunk = None
                except StopAsyncIteration:
                    break
                if chunk["event"] == "delta":
                    response_text += chunk.get("text", "")
                elif chunk["event"] == "thinking":
                    response_thinking += chunk.get("text", "")
                elif chunk["event"] == "tool_use":
                    response_traces.append({
                        "type": "tool_use",
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "input": chunk.get("input"),
                        "text_offset": len(response_text.rstrip()),
                    })
                elif chunk["event"] == "tool_result":
                    response_traces.append({
                        "type": "tool_result",
                        "tool_use_id": chunk.get("tool_use_id"),
                        "content": trace_content(chunk.get("content")),
                        "is_error": chunk.get("is_error", False),
                    })
                elif chunk["event"] == "done":
                    logger.info(
                        "claude_raw_response request_id=%s conv_id=%s raw=%r",
                        request_id,
                        conv_id,
                        response_text,
                    )
                    if response_traces and not response_thinking:
                        try:
                            trace_sum = await summarize_traces(
                                response_traces,
                            )
                            if trace_sum:
                                response_traces.insert(0, {
                                    "type": "summary",
                                    "text": trace_sum,
                                })
                                ts_data = json.dumps(
                                    {"text": trace_sum},
                                    ensure_ascii=False,
                                )
                                yield f"event: trace_summary\ndata: {ts_data}\n\n"
                        except Exception:
                            logger.exception("trace summary failed")
                    assistant_message_id = complete_turn(
                        conv_id,
                        chunk["session_id"],
                        response_text,
                        response_thinking,
                        response_traces,
                    )
                    chunk["conversation_id"] = conv_id
                    chunk["assistant_message_id"] = assistant_message_id
                    branch_committed = True
                name = chunk.pop("event")
                data = json.dumps(chunk, ensure_ascii=False)
                yield f"event: {name}\ndata: {data}\n\n"
        except ConversationNotFound:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话不存在或已被删除"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ValueError as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc) or "这条消息不能这样操作"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except SessionResumeError:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话恢复失败"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ActorBusyError as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc)},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except anthropic.APIError as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            status = getattr(exc, "status_code", None)
            logger.warning("API request failed: type=%s status=%s", type(exc).__name__, status)
            if status in {401, 403}:
                message = "API 认证或权限失败，请检查 key、服务商权限和访问限制"
            elif status == 404:
                message = "API 地址或模型不存在，请检查 base URL 和 models.json 中的模型 ID"
            elif status == 400:
                message = "API 不接受当前参数，请检查模型、Anthropic 协议兼容性，或关闭扩展思考后重试"
            elif status == 429:
                message = "API 请求受限，请检查额度或稍后重试"
            elif isinstance(exc, anthropic.APIConnectionError):
                message = "无法连接 API，请检查后端所在机器的网络、代理和服务商地址"
            else:
                message = "API 服务暂时不可用，请稍后重试"
            payload = json.dumps({"message": message}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
        except Exception as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            logger.exception("Chat backend request failed")
            detail = str(exc)
            if "not available" in detail.lower() or "invalid model" in detail.lower():
                message = f"所选模型当前不可用：{body.model}"
            else:
                message = "模型暂时没有响应，请稍后重试。"
            payload = json.dumps(
                {"message": message},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            try:
                if pending_chunk is not None:
                    pending_chunk.cancel()
                    await asyncio.gather(pending_chunk, return_exceptions=True)
                if chunk_iter is not None:
                    await chunk_iter.aclose()
            finally:
                chat_lock.release()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/thinking-summary", dependencies=[Depends(require_auth)])
async def thinking_summary(body: ThinkingSummaryBody) -> dict:
    try:
        summary = (await summarize_api(body.thinking) if CHAT_BACKEND == "api"
                   else await summarize_thinking(body.thinking))
    except Exception:
        logger.exception("thinking summary failed")
        summary = ""
    return {"summary": summary}


@app.post("/api/tool-caption", dependencies=[Depends(require_auth)])
async def tool_caption(body: ToolCaptionBody) -> dict:
    try:
        caption = (await summarize_api(f"{body.tool_name}\n{body.tool_input}\n{body.tool_output}")
                   if CHAT_BACKEND == "api" else
                   await summarize_tool_use(body.tool_name, body.tool_input, body.tool_output))
    except Exception:
        logger.exception("tool caption failed")
        caption = ""
    return {"caption": caption}


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(
    files: list[UploadFile] = File(...),
    conversation_id: str | None = Form(default=None),
) -> dict:
    conv_id = ensure_conversation(conversation_id)
    attachments = await save_uploads(conv_id, files)
    return {
        "conversation_id": conv_id,
        "attachments": attachments,
    }


@app.get(
    "/api/uploads/{conversation_id}/{filename}",
    dependencies=[Depends(require_auth)],
)
async def uploaded_file(conversation_id: str, filename: str) -> FileResponse:
    path = validated_file(conversation_id, filename)
    return FileResponse(path)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def sessions() -> dict:
    return {"sessions": session_list()}


@app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def messages(
    session_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict:
    try:
        items, has_more, next_before_id = session_messages(session_id, before_id, limit)
        return {
            "messages": items,
            "has_more": has_more,
            "next_before_id": next_before_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.patch("/api/sessions/{session_id}/title", dependencies=[Depends(require_auth)])
async def rename(session_id: str, body: RenameBody) -> dict:
    set_session_title(session_id, body.title)
    return {"renamed": True}


@app.patch("/api/sessions/{session_id}/star", dependencies=[Depends(require_auth)])
async def star(session_id: str, body: StarBody) -> dict:
    set_session_starred(session_id, body.starred)
    return {"starred": body.starred}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete(session_id: str) -> dict:
    await get_registry().invalidate(resolve_conversation(session_id))
    remove_session(session_id)
    remove_conversation_uploads(session_id)
    return {"deleted": True}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def get_memory() -> dict:
    return {"content": read_memory()}


@app.put("/api/memory", dependencies=[Depends(require_auth)])
async def put_memory(body: MemoryBody) -> dict:
    write_memory(body.content)
    await get_registry().invalidate()
    return {"saved": True}


@app.get("/api/profile", dependencies=[Depends(require_auth)])
async def get_profile() -> dict:
    profile, imported_count, found_count = import_claude_export_memories(read_profile())
    if imported_count:
        await get_registry().invalidate()
    return {
        "profile": profile,
        "importedCount": imported_count,
        "foundCount": found_count,
    }


@app.put("/api/profile", dependencies=[Depends(require_auth)])
async def put_profile(body: ProfileBody) -> dict:
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    profile = write_profile(data)
    await get_registry().invalidate()
    return {"saved": True, "profile": profile}


@app.post("/api/profile/memory", dependencies=[Depends(require_auth)])
async def post_memory(body: dict) -> dict:
    content = body.get("content", "").strip()
    if not content:
        return {"saved": False, "reason": "empty content"}
    result = add_saved_memory(content)
    if result is None:
        return {"saved": False, "reason": "duplicate or limit reached"}
    return {"saved": True, "memory": result}


@app.get("/api/diary", dependencies=[Depends(require_auth)])
async def get_diary() -> dict:
    entries = read_diary_entries()
    return {"entries": entries}


@app.get("/api/splash")
async def splash() -> dict:
    period = current_period()
    return {"period": period, "line": random_line(period)}


@app.get("/api/models")
async def models() -> dict:
    return {"models": available_models()}
