"""验证本地文档预览、聊天配置与流式历史。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from backend.ai import ChatHistory
from backend.config import HealthConfig, Settings
from backend.health import HealthStore
from backend.main import create_app
from backend.scratchpad import PREVIEW_LIMIT, Scratchpad


def local_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost")


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_is_inline_only_when_requested_and_valid(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            Scratchpad(root).initialize()
            app = create_app(Settings(), root)
            async with local_client(app) as client:
                async def upload(name, value):
                    response = await client.post("/api/scratchpad/file", files={"file": (name, value, "application/pdf")})
                    self.assertEqual(response.status_code, 201)
                    return f"/api/scratchpad/files/{response.json()['id']}"

                data = b"%PDF-1.7\nfixture"
                pdf = await upload("报告.pdf", data)
                opened = await client.get(pdf + "?preview=true")
                self.assertEqual(opened.status_code, 200)
                self.assertEqual(opened.content, data)
                self.assertEqual(opened.headers["content-type"], "application/pdf")
                self.assertEqual(opened.headers["x-content-type-options"], "nosniff")
                self.assertTrue(opened.headers["content-disposition"].startswith("inline;"))
                for suffix in ("", "?preview=true&download=true"):
                    response = await client.get(pdf + suffix)
                    self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                fake = await upload("fake.pdf", b"<html>arbitrary upload</html>")
                response = await client.get(fake + "?preview=true")
                self.assertEqual(response.headers["content-type"], "application/octet-stream")
                self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                large = await upload("large.pdf", data + b"0" * (PREVIEW_LIMIT - len(data) + 1))
                response = await client.get(large + "?preview=true")
                self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_persist_only_the_two_public_flags(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "config.json"
            initial = {"port": 4123, "ai": {"api_key": "private-key", "base_url": "https://api.openai.com/v1",
                                            "model": "gpt-6-luna", "enabled": False}}
            path.write_text(json.dumps(initial), encoding="utf-8")
            app = create_app(data_dir=root, config_path=path)
            async with local_client(app) as client:
                before = await client.get("/api/ai/settings")
                self.assertEqual(before.json(), {"enabled": False, "include_vitals_context": True, "configured": True})
                result = await client.patch("/api/ai/settings", json={"enabled": True, "include_vitals_context": False})
                self.assertEqual(result.json(), {"enabled": True, "include_vitals_context": False, "configured": True})
                self.assertEqual((await client.get("/api/ai/status")).json(), {"enabled": True})
                for update in ({"api_key": "replace"}, {"base_url": "https://example.org"}, {"enabled": "true"}):
                    self.assertEqual((await client.patch("/api/ai/settings", json=update)).status_code, 422)
            fresh = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(fresh, {**initial, "ai": {**initial["ai"], "enabled": True, "include_vitals_context": False}})

    async def test_chat_stream_persists_and_syncs_and_can_be_cleared(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Scratchpad(root)
            store.initialize()
            store.add_text("synthetic scratchpad item")
            app = create_app(Settings(ai={"enabled": True, "api_key": "test-key", "include_vitals_context": False}), root)
            calls = []

            def provider(request):
                self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
                self.assertEqual(request.headers["authorization"], "Bearer test-key")
                calls.append(json.loads(request.content))
                chunks = ['data: {"choices":[{"delta":{"content":"你好"}}]}',
                          'data: {"choices":[{"delta":{"content":"，测试成功。"}}]}', "data: [DONE]"]
                return httpx.Response(200, content=("\n\n".join(chunks) + "\n\n").encode())

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external, local_client(app) as client:
                app.state.external_client = external
                response = await client.post("/api/ai/chat", json={"message": "  hello  "})
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.headers["content-type"])
                self.assertIn("event: delta", response.text)
                self.assertIn("event: done", response.text)
                self.assertEqual(calls[0]["model"], "gpt-6-luna")
                self.assertTrue(calls[0]["stream"])
                self.assertIn("max_completion_tokens", calls[0])
                self.assertEqual(calls[0]["reasoning_effort"], "none")
                self.assertNotIn("scratchpad", calls[0]["messages"][0]["content"])
                self.assertEqual(calls[0]["messages"][-1]["content"], "hello")
                await client.post("/api/ai/chat", json={"message": "整理中转站最新内容"})
                self.assertIn("synthetic scratchpad item", calls[1]["messages"][-2]["content"])
                self.assertEqual(calls[1]["messages"][-2]["role"], "user")
                self.assertEqual(calls[1]["messages"][1]["content"], "hello")
                async with local_client(app) as other:
                    items = (await other.get("/api/ai/messages")).json()
                    self.assertEqual([item["role"] for item in items], ["user", "assistant"] * 2)
                    self.assertEqual(items[1]["content"], "你好，测试成功。")
                    self.assertEqual((await other.delete("/api/ai/messages")).status_code, 204)
                self.assertEqual((await client.get("/api/ai/messages")).json(), [])

    async def test_scratchpad_context_requires_a_request_for_records(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Scratchpad(root)
            store.initialize()
            for index in range(6):
                store.add_text(f"private record {index}")
            app = create_app(Settings(ai={"enabled": True, "api_key": "test-key", "include_vitals_context": False}), root)
            calls = []

            def provider(request):
                calls.append(json.loads(request.content)["messages"])
                return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external, local_client(app) as client:
                app.state.external_client = external
                for prompt in ("帮我看看中转站刚存的东西", "请总结一下中转站里最近的记录！", "中转站最新内容，帮我整理一下"):
                    with self.subTest(prompt=prompt):
                        await client.post("/api/ai/chat", json={"message": prompt})
                        record_text = calls[-1][-2]["content"]
                        self.assertIn("private record 5", record_text)
                        self.assertNotIn("private record 0", record_text)
                for prompt in ("中转站怎么用？", "不要帮我查看中转站的内容", "‘整理中转站最新内容’会发送什么？"):
                    with self.subTest(prompt=prompt):
                        await client.post("/api/ai/chat", json={"message": prompt})
                        self.assertFalse(any("private record" in item["content"] for item in calls[-1]))

    async def test_persona_reloads_in_both_routes_with_an_alternative_official_model(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            agent_file = root / "agent.md"
            store = Scratchpad(root)
            store.initialize()
            note = store.add_text("Synthetic note")
            settings = Settings(ai={"enabled": True, "api_key": "test-key", "include_vitals_context": False,
                                    "model": " gpt-6-sol "})
            self.assertTrue(settings.ai.configured)
            app = create_app(settings, root)
            calls = []

            def provider(request):
                self.assertEqual(request.url.host, "api.openai.com")
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], "gpt-6-sol")
                self.assertNotIn("reasoning_effort", payload)
                calls.append(payload["messages"][0]["content"])
                if payload["stream"]:
                    return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
                return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

            with patch("backend.ai.AGENT_PATH", agent_file):
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external, local_client(app) as client:
                    app.state.external_client = external
                    agent_file.write_text("Persona v1", encoding="utf-8")
                    await client.post("/api/ai/chat", json={"message": "Hello"})
                    self.assertIn("Persona v1", calls[-1])
                    agent_file.write_text("Persona v2", encoding="utf-8")
                    await client.post("/api/ai/chat", json={"message": "Hello again"})
                    self.assertIn("Persona v2", calls[-1])
                    agent_file.write_text("Persona v3", encoding="utf-8")
                    response = await client.post("/api/ai/analyze", json={"id": note["id"]})
                    self.assertEqual(response.json(), {"result": "ok"})
                    self.assertIn("Persona v3", calls[-1])
                    agent_file.unlink()
                    await client.post("/api/ai/analyze", json={"id": note["id"]})
                    self.assertNotIn("Persona v3", calls[-1])
            self.assertFalse(settings.ai.model_copy(update={"base_url": Settings(ai={"base_url": "https://example.com/v1"}).ai.base_url}).configured)

    async def test_provider_error_and_missing_credentials_do_not_create_history(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            Scratchpad(root).initialize()
            settings = Settings(ai={"enabled": True, "api_key": "test-key", "include_vitals_context": False})
            app = create_app(settings, root)
            async with local_client(app) as client, httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(429, text="secret test-key")),
            ) as external:
                app.state.external_client = external
                result = await client.post("/api/ai/chat", json={"message": "hello"})
                self.assertIn("event: error", result.text)
                self.assertNotIn("test-key", result.text)
                self.assertEqual((await client.get("/api/ai/messages")).json(), [])
                await client.patch("/api/ai/settings", json={"enabled": False})
                self.assertEqual((await client.post("/api/ai/chat", json={"message": "hello"})).status_code, 503)
            legacy = Settings(ai={"enabled": True, "api_key": "legacy", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"})
            async with local_client(create_app(legacy, root)) as client:
                self.assertFalse((await client.get("/api/ai/settings")).json()["configured"])
                self.assertEqual((await client.post("/api/ai/chat", json={"message": "hello"})).status_code, 503)

    async def test_context_is_narrow_and_history_keeps_the_latest_50(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Scratchpad(root)
            store.initialize()
            health = HealthStore(store.connect, HealthConfig())
            health.initialize()
            history = ChatHistory(store)
            for index in range(28):
                history.save(f"q{index}", f"a{index}")
            self.assertEqual(len(history.messages()), 50)
            self.assertEqual(history.messages()[0]["content"], "q3")
            app = create_app(Settings(ai={"enabled": True, "api_key": "test-key"}), root)
            app.state.monitor = SimpleNamespace(latest={"cpu": {"usage_percent": 42, "temperature_c": 51},
                "network": {"interfaces": [{"addresses": ["secret-address"]}]}, "gpus": [], "memory": {"percent": 60}})

            def provider(request):
                if request.url.host != "api.openai.com":
                    return httpx.Response(503)
                messages = json.loads(request.content)["messages"]
                snapshot = messages[-2]
                self.assertEqual(snapshot["role"], "user")
                self.assertIn('"cpu_percent": 42', snapshot["content"])
                self.assertNotIn("secret-address", snapshot["content"])
                self.assertEqual(len(messages), 15)
                return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external, local_client(app) as client:
                app.state.external_client = external
                reply = await client.post("/api/ai/chat", json={"message": "context"})
                self.assertIn("event: done", reply.text)


def test_sleep_hourly_uses_observed_segments_without_double_counting(tmp_path):
    store = Scratchpad(tmp_path)
    store.initialize()
    health = HealthStore(store.connect, HealthConfig())
    health.initialize()
    health.ingest({"data": {"metrics": [{"name": "sleep_analysis", "data": [
        {"start": "2026-01-03T00:30:00-05:00", "end": "2026-01-03T02:15:00-05:00", "value": "Core"},
        {"start": "2026-01-03T00:45:00-05:00", "end": "2026-01-03T01:15:00-05:00", "value": "Awake"},
    ]}]}}, 2)
    sleep = health.detail("sleep", datetime(2026, 1, 3, 17, tzinfo=timezone.utc))
    assert [hour["sleep_minutes"] for hour in sleep["hourly"]] == [15, 45, 15]
