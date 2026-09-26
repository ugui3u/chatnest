"""Offline integration tests: isolated state, real Anthropic SDK, mocked HTTP."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_STATE = tempfile.TemporaryDirectory()
ROOT = Path(__file__).resolve().parents[1]
os.environ.update({
    "AGENT_APP_ROOT": _STATE.name,
    "CONVERSATION_DB": str(Path(_STATE.name) / "conversations.db"),
    "CLAUDE_SESSION_DIR": str(Path(_STATE.name) / "sessions"),
    "MODELS_FILE": str(ROOT / "models.json"),
    "CHAT_PASSWORD": "test-password",
    "CHAT_SECRET": "test-secret",
    "CHAT_BACKEND": "api",
    "ANTHROPIC_API_KEY": "offline-test-key",
    "ANTHROPIC_BASE_URL": "https://provider.example/v1",
    "API_SUMMARY_MODEL": "",
})

import anthropic
import httpx
from fastapi.testclient import TestClient
from app import main, claude_api as api
from app.store import conversation_messages


def events(response):
    result = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        if len(lines) >= 2 and lines[0].startswith("event: "):
            result.append((lines[0][7:], json.loads(lines[1][6:])))
    return result


class BackendsTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.clients = []
        self.patches = [
            patch.object(main, "CHAT_BACKEND", "api"),
            patch.object(main, "recall_memory", return_value="test memory"),
            patch.object(api, "build_system_prompt", new=AsyncMock(return_value="test system")),
            patch.object(api, "_get_client", side_effect=self.api_client),
            patch.object(main, "stream_chat", side_effect=AssertionError("API must not invoke CC")),
        ]
        for p in self.patches:
            p.start()
        self.web = TestClient(main.app)
        self.web.__enter__()
        self.headers = {"Authorization": "Bearer " + main.auth.issue_token("test-password")}

    def tearDown(self):
        self.web.__exit__(None, None, None)
        for p in reversed(self.patches):
            p.stop()

    def api_client(self):
        key, base = api.validate_api_settings()
        client = anthropic.AsyncAnthropic(
            api_key=key, base_url=base, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )
        self.clients.append(client)
        return client

    def handle(self, request):
        self.assertEqual(request.url.path, "/v1/messages")
        self.assertEqual(request.headers["x-api-key"], "offline-test-key")
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error", "error": {"type": "invalid_request_error", "message": "test failure"}})
        stream = [
            {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": payload["model"], "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello from API"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ]
        data = "".join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=data)

    def chat(self, **kwargs):
        response = self.web.post("/api/chat", headers=self.headers, json={"message": "first", "extended": False, **kwargs})
        self.assertEqual(response.status_code, 200)
        return events(response)

    def test_api_two_turns_and_current_context_once(self):
        first = dict(self.chat())
        self.assertEqual(first["delta"]["text"], "hello from API")
        conv = first["done"]["conversation_id"]
        second = dict(self.chat(message="second", conversation_id=conv))
        self.assertIn("done", second)
        history = self.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "user"])
        self.assertEqual(history[0]["content"][0]["text"], "first")
        self.assertEqual(history[2]["content"][0]["text"].count("second"), 1)
        self.assertNotIn("thinking", self.requests[-1])
        self.assertTrue(all(c.is_closed() for c in self.clients))

    def test_edit_and_retry_rebuild_only_active_history(self):
        first = dict(self.chat())
        conv = first["done"]["conversation_id"]
        self.chat(message="second", conversation_id=conv)
        edited = dict(self.chat(message="edited", conversation_id=conv, edit_message_id=first["conversation"]["user_message_id"]))
        self.assertIn("done", edited)
        self.assertEqual(len(self.requests[-1]["messages"]), 1)
        self.assertIn("edited", self.requests[-1]["messages"][0]["content"][0]["text"])
        retried = dict(self.chat(message="", conversation_id=conv, retry_message_id=edited["done"]["assistant_message_id"]))
        self.assertIn("done", retried)
        self.assertEqual(len(self.requests[-1]["messages"]), 1)

    def test_history_omits_unsigned_thinking(self):
        first = dict(self.chat())
        conv = first["done"]["conversation_id"]
        from app.store import _connect
        with _connect() as db:
            db.execute("UPDATE messages SET thinking = 'private thinking' WHERE conv_id = ? AND role = 'assistant'", (conv,))
        self.chat(message="next", conversation_id=conv)
        self.assertNotIn("private thinking", json.dumps(self.requests[-1]))

    def test_uploaded_text_is_sent_as_content(self):
        conv = main.ensure_conversation(None)
        uploaded = self.web.post("/api/upload", headers=self.headers, data={"conversation_id": conv}, files={"files": ("note.txt", b"attachment contents", "text/plain")})
        self.assertEqual(uploaded.status_code, 200)
        path = uploaded.json()["attachments"][0]["path"]
        result = dict(self.chat(message="read this", conversation_id=conv, attachments=[path]))
        self.assertIn("done", result)
        self.assertIn("attachment contents", json.dumps(self.requests[-1]))
        self.assertNotIn(path, json.dumps(self.requests[-1]))

    def test_auth_error_is_actionable_and_does_not_complete_turn(self):
        self.status = 401
        result = dict(self.chat())
        self.assertIn("认证", result["error"]["message"])
        self.assertNotIn("done", result)
        rows, _, _ = conversation_messages(result["conversation"]["conversation_id"])
        self.assertEqual([r["role"] for r in rows], ["user"])
        self.assertTrue(all(c.is_closed() for c in self.clients))

    def test_missing_key_does_not_create_turn(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = dict(self.chat())
        self.assertIn("ANTHROPIC_API_KEY", result["error"]["message"])
        self.assertNotIn("conversation", result)
        self.assertEqual(self.requests, [])

    def test_captions_do_not_call_cc_in_api_mode(self):
        with patch.object(main, "summarize_thinking", side_effect=AssertionError("CC caption")):
            result = self.web.post("/api/thinking-summary", headers=self.headers, json={"thinking": "test"})
        self.assertEqual(result.json(), {"summary": ""})
        with patch.object(main, "summarize_tool_use", side_effect=AssertionError("CC caption")):
            result = self.web.post("/api/tool-caption", headers=self.headers, json={"tool_name": "Read"})
        self.assertEqual(result.json(), {"caption": ""})
        self.assertEqual(self.requests, [])

    def test_sdk_and_codex_routes_are_preserved(self):
        calls = []
        async def fake(*args):
            calls.append(args)
            yield {"event": "delta", "text": "CLI reply"}
            yield {"event": "done", "session_id": "cli-test"}
        with patch.object(main, "CHAT_BACKEND", "sdk"), patch.object(main, "stream_chat", new=fake):
            self.assertIn("done", dict(self.chat()))
        with patch.object(main, "stream_codex_chat", new=fake):
            self.assertIn("done", dict(self.chat(model="codex")))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.requests, [])

    def test_api_to_sdk_preserves_visible_history(self):
        first = dict(self.chat())
        calls = []
        async def fake(*args):
            calls.append(args)
            yield {"event": "done", "session_id": "sdk-new"}
        with patch.object(main, "CHAT_BACKEND", "sdk"), patch.object(main, "stream_chat", new=fake):
            result = dict(self.chat(message="continue", conversation_id=first["done"]["conversation_id"]))
        self.assertIn("done", result)
        self.assertIsNone(calls[0][2])
        self.assertIn("hello from API", calls[0][0])

    def test_adaptive_thinking_parameters(self):
        self.chat(extended=True, effort="high")
        self.assertEqual(self.requests[-1]["thinking"], {"type": "adaptive"})
        self.assertEqual(self.requests[-1]["output_config"], {"effort": "high"})

    def test_heartbeat_does_not_cancel_slow_api_stream(self):
        original_wait = asyncio.wait
        async def fast_wait(tasks, *, timeout):
            return await original_wait(tasks, timeout=0.001)
        async def slow_stream(*args, **kwargs):
            await asyncio.sleep(0.01)
            yield {"event": "delta", "text": "slow reply"}
            await asyncio.sleep(0.01)
            yield {"event": "done", "session_id": "api-slow"}
        with patch.object(main, "stream_chat_api", new=slow_stream), patch.object(main.asyncio, "wait", new=fast_wait):
            response = self.web.post("/api/chat", headers=self.headers, json={"message": "slow"})
        self.assertIn(": heartbeat", response.text)
        self.assertIn("done", dict(events(response)))
        self.assertIn("slow reply", response.text)

    def test_attachment_path_cannot_escape_uploads(self):
        secret = Path(_STATE.name) / "outside.txt"
        secret.write_text("do not send")
        with self.assertRaises(ValueError):
            api._attachment_blocks([{"path": str(secret)}])

    def test_base_url_rejects_full_endpoint_and_credentials(self):
        for url in ["https://provider.example/v1/messages", "https://secret@provider.example", "not-a-url"]:
            with self.subTest(url=url), patch.dict(os.environ, {"ANTHROPIC_BASE_URL": url}):
                with self.assertRaises(ValueError):
                    api.validate_api_settings()


if __name__ == "__main__":
    unittest.main()
