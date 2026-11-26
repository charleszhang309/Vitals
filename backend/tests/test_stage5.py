"""每日简报、手机推送、快捷控制与健康周报回归。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import httpx

from backend.ai import AiState, DailyBriefing
from backend.config import HealthConfig, Settings, WebhookConfig
from backend.health import HealthStore
from backend.main import create_app
from backend.monitor import Rules, send_webhook
from backend.scratchpad import Scratchpad


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost")


def data(metric, units, *samples):
    return {"name": metric, "units": units, "data": list(samples)}


class Clock(datetime):
    instant = datetime(2026, 11, 25, 4, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz)


class BriefingTests(IsolatedAsyncioTestCase):
    async def test_scheduled_bark_uses_local_window_and_persistent_daily_claim(self):
        with TemporaryDirectory() as directory:
            store = Scratchpad(Path(directory))
            store.initialize()
            health = HealthStore(store.connect, HealthConfig(timezone="America/Toronto"))
            state = AiState(Settings(ai={"enabled": True, "api_key": "fixture-key"}).ai, None)
            webhook = WebhookConfig(kind="bark", daily_briefing_time="08:00",
                                    bark_url_template="https://bark.test/fixture-key/{title}/{body}")
            day = "2026-11-25"
            at_eight = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)
            calls = []

            def provider(request):
                self.assertEqual(request.url.host, "bark.test")
                calls.append(request.url.path)
                return httpx.Response(200, json={"code": 200})

            briefing = DailyBriefing(store, health, None)
            briefing.save(day, "今天的测试简报。", at_eight.isoformat())
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external:
                await briefing.bark_if_due(external, state, webhook, now=at_eight - timedelta(seconds=1))
                await briefing.bark_if_due(external, state, webhook, now=at_eight + timedelta(hours=1))
                state.update({"include_vitals_context": False})
                await briefing.bark_if_due(external, state, webhook, now=at_eight)
                self.assertEqual(calls, [])
                state.update({"include_vitals_context": True})
                await asyncio.gather(*(briefing.bark_if_due(external, state, webhook, now=at_eight) for _ in range(2)))
                self.assertEqual(calls, ["/fixture-key/明日香的每日简报/今天的测试简报。"])
                briefing.save(day, "刷新后的测试简报。", (at_eight + timedelta(minutes=2)).isoformat())
                store.initialize()
                restarted = DailyBriefing(store, health, None)
                await restarted.bark_if_due(external, state, webhook, now=at_eight + timedelta(minutes=4))
                self.assertEqual(len(calls), 1)
                self.assertEqual(restarted.cached(day)["content"], "刷新后的测试简报。")

    async def test_daily_cache_refresh_local_rollover_and_privacy_switch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            Scratchpad(root).initialize()
            health = HealthStore(Scratchpad(root).connect, HealthConfig(timezone="America/Toronto"))
            health.initialize()
            health.ingest({"data": {"metrics": [
                data("step_count", "count", {"date": "2026-11-24T18:15:00Z", "qty": 812}),
                data("sleep_analysis", "hr", {"date": "2026-11-24T12:00:00Z", "sleepStart": "2026-11-24T03:30:00Z",
                     "sleepEnd": "2026-11-24T12:00:00Z", "totalSleep": 7},
                     {"date": "2026-11-24T18:00:00Z", "sleepStart": "2026-11-24T17:15:00Z",
                      "sleepEnd": "2026-11-24T18:00:00Z", "totalSleep": .75}),
            ]}}, 2)
            settings = Settings(health={"timezone": "America/Toronto"}, ai={"enabled": True, "api_key": "fixture-key"})
            app = create_app(settings, root)
            contexts = []
            upstream_ok = True

            def provider(request):
                if request.url.host != "api.openai.com":
                    return httpx.Response(503)
                if not upstream_ok:
                    return httpx.Response(502)
                messages = json.loads(request.content)["messages"]
                self.assertIn("明日香", messages[0]["content"])
                contexts.append(json.loads(messages[1]["content"]))
                return httpx.Response(200, json={"choices": [{"message": {"content": f"简报 {len(contexts)}"}}]})

            with patch("backend.ai.datetime", Clock), patch.object(Clock, "instant", Clock.instant):
                async with client_for(app) as client, httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external:
                    app.state.external_client = external
                    first, concurrent = await asyncio.gather(client.get("/api/ai/briefing"), client.get("/api/ai/briefing"))
                    self.assertEqual(first.json(), concurrent.json())
                    self.assertEqual(first.headers["cache-control"], "no-store")
                    self.assertEqual(first.json()["date"], "2026-11-24")
                    self.assertEqual(contexts[0]["health_today"]["step_count"], 812)
                    self.assertEqual(contexts[0]["sleep_minutes"], 420)
                    self.assertEqual(len(contexts), 1)

                    restarted = create_app(settings, root)
                    restarted.state.external_client = external
                    async with client_for(restarted) as reloaded:
                        self.assertEqual((await reloaded.get("/api/ai/briefing")).json(), first.json())
                    Clock.instant = datetime(2026, 11, 25, 4, 40, tzinfo=timezone.utc)
                    refreshed = await client.get("/api/ai/briefing?refresh=1")
                    self.assertEqual(refreshed.json()["content"], "简报 2")
                    Clock.instant = datetime(2026, 11, 25, 4, 45, tzinfo=timezone.utc)
                    upstream_ok = False
                    stale = await client.get("/api/ai/briefing?refresh=1")
                    self.assertTrue(stale.json()["refresh_failed"])
                    self.assertEqual(stale.json()["content"], "简报 2")
                    upstream_ok = True
                    cross = await client.get("/api/ai/briefing?refresh=1", headers={"Origin": "https://elsewhere.test", "Sec-Fetch-Site": "cross-site"})
                    self.assertEqual(cross.status_code, 403)

                    Clock.instant = datetime(2026, 11, 25, 5, 1, tzinfo=timezone.utc)
                    rolled = await client.get("/api/ai/briefing")
                    self.assertEqual(rolled.json()["date"], "2026-11-25")
                    self.assertIsNone(contexts[-1]["health_today"]["step_count"])
                    self.assertIsNone(contexts[-1]["sleep_minutes"])
                    await client.patch("/api/ai/settings", json={"include_vitals_context": False})
                    self.assertEqual((await client.get("/api/ai/briefing")).json(), {"available": False})
                    await client.patch("/api/ai/settings", json={"enabled": False, "include_vitals_context": True})
                    self.assertEqual((await client.get("/api/ai/briefing")).json(), {"available": False})
                    self.assertEqual(len(contexts), 3)
                async with client_for(create_app(Settings(ai={"enabled": True}), root)) as unconfigured:
                    self.assertEqual((await unconfigured.get("/api/ai/briefing")).json(), {"available": False})


class QuickControlTests(IsolatedAsyncioTestCase):
    async def test_whitelist_confirmation_schedule_and_cancel(self):
        with TemporaryDirectory() as directory:
            with patch("backend.main.host_identity", return_value={"platform": "windows", "name": "Fixture"}):
                app = create_app(Settings(), Path(directory))
            async with client_for(app) as client:
                with patch("backend.control.dispatch_desktop") as dispatch:
                    for payload in ({"action": "probe"}, {"action": "cmd.exe"}, {"action": "lock", "confirmed": 1},
                                    {"action": "display-off", "args": ["anything"]}):
                        self.assertEqual((await client.post("/api/control/desktop", json=payload)).status_code, 422)
                    self.assertEqual((await client.post("/api/control/desktop", json={"action": "lock"})).status_code, 409)
                    dispatch.assert_not_called()
                    for action in ("lock", "display-off"):
                        result = await client.post("/api/control/desktop", json={"action": action, "confirmed": action == "lock"})
                        self.assertEqual(result.json(), {"accepted": True, "action": action})
                    self.assertEqual([call.args for call in dispatch.call_args_list], [("lock",), ("display-off",)])
                with patch("backend.control.os.name", "nt"), patch("backend.control.shutdown_windows", Mock()) as shutdown:
                    self.assertEqual((await client.post("/api/control/shutdown", json={"minutes": 30})).status_code, 409)
                    scheduled = await client.post("/api/control/shutdown", json={"minutes": 30, "confirmed": True})
                    self.assertEqual(scheduled.status_code, 200)
                    self.assertEqual((await client.get("/api/control/quick")).json(), scheduled.json())
                    self.assertEqual((await client.delete("/api/control/shutdown")).json(), {"shutdown_at": None})
                    shutdown.assert_not_called()


class BarkTests(IsolatedAsyncioTestCase):
    async def test_test_push_and_existing_rules_share_bark_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(webhook={"kind": "bark", "bark_url_template": "https://bark.test/private-value/{title}/{body}"},
                                alerts={"temperature_seconds": 1}, task={"high_seconds": 1, "low_seconds": 1})
            app = create_app(settings, root)
            calls = []
            code = 200

            def provider(request):
                self.assertEqual(request.method, "GET")
                self.assertTrue(request.url.path.startswith("/private-value/"))
                calls.append(request.url.path.split("/", 3)[2])
                return httpx.Response(200, json={"code": code, "message": "private-value"})

            async with client_for(app) as client, httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external:
                app.state.external_client = external
                response = await client.post("/api/control/webhook/test")
                self.assertEqual(response.json(), {"sent": True, "provider": "bark"})
                code = 400
                rejected = await client.post("/api/control/webhook/test")
                self.assertEqual(rejected.status_code, 502)
                self.assertNotIn("private-value", rejected.text)
                code = 200
                sample = {"timestamp": "2026-11-25T00:00:00Z", "cpu": {"usage_percent": 95},
                          "gpus": [{"id": "gpu", "usage_percent": 95, "temperature_c": 95}],
                          "disks": [{"mountpoint": "X", "free_bytes": 1}]}
                rules = Rules(settings)
                events = rules.evaluate(sample, {"cpu": 95}, 0)
                events += rules.evaluate(sample, {"cpu": 95}, 2)
                sample["cpu"]["usage_percent"] = 1
                sample["gpus"][0]["usage_percent"] = 1
                events += rules.evaluate(sample, {}, 3)
                events += rules.evaluate(sample, {}, 4)
                self.assertEqual({event["type"] for event in events}, {"cpu_temperature", "gpu_temperature", "disk_space", "task_completed"})
                for event in events:
                    await send_webhook(external, settings.webhook, event)
                self.assertEqual(set(calls[2:]), {"CPU 温度告警", "GPU 温度告警", "磁盘空间告警", "任务已完工"})
            async with client_for(create_app(Settings(), root)) as client:
                self.assertEqual((await client.post("/api/control/webhook/test")).status_code, 409)


def test_weekly_comparison_uses_complete_local_days_and_observed_samples(tmp_path):
    store = Scratchpad(tmp_path)
    store.initialize()
    health = HealthStore(store.connect, HealthConfig(timezone="America/Toronto"))
    health.initialize()
    def row(day, qty, hour="12:00:00"):
        return {"date": f"2026-11-{day:02}T{hour}-05:00", "qty": qty}

    health.ingest({"data": {"metrics": [
        data("step_count", "count", row(10, 100), row(17, 1000), row(18, 1100), row(18, 300, "14:00:00"),
             row(24, 1600), row(25, 9999, "01:00:00")),
        data("resting_heart_rate", "count/min", row(24, 57), row(24, 61, "14:00:00"), row(23, 63)),
        data("sleep_analysis", "hr", {**row(17, 0), "totalSleep": 6, "sleepEnd": row(17, 0)["date"]},
             {**row(24, 0), "totalSleep": 7.5, "sleepEnd": row(24, 0)["date"]}),
    ]}}, 2)
    result = health.weekly_comparison(datetime(2026, 11, 25, 7, tzinfo=timezone.utc))
    assert result["recent"] == {"start": "2026-11-18", "end": "2026-11-24"}
    assert result["previous"] == {"start": "2026-11-11", "end": "2026-11-17"}
    assert result["metrics"]["steps"] == {"recent": 1500, "previous": 1000, "delta": 500,
                                           "recent_days": 2, "previous_days": 1, "percent_change": 50}
    assert result["metrics"]["sleep_minutes"]["delta"] == 90
    resting = result["metrics"]["resting_heart_rate"]
    assert resting["recent"] == 61 and resting["delta"] is None and resting["previous_days"] == 0
