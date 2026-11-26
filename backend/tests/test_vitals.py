"""覆盖 Vitals 的本地持久化、外部服务退避与健康导入。"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
import httpx

from backend.ai import execute_command
from backend.config import HealthConfig, Settings, WeatherConfig
from backend.control import PowerControl
from backend.health import HealthStore
from backend.main import create_app
from backend.scratchpad import Scratchpad
from backend.weather import FRESH_SECONDS, RETRY_SECONDS, Weather


def api_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost")


class TransitTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_records_migrate_pin_and_cleanup_file(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Scratchpad(root)
            with store.connect() as db:
                db.execute("""CREATE TABLE scratchpad (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    text TEXT, filename TEXT, size_bytes INTEGER, mime_type TEXT, storage_key TEXT UNIQUE, created_at TEXT NOT NULL)""")
                db.execute("INSERT INTO scratchpad(kind,text,created_at) VALUES ('text','原有文本','2025-01-01T00:00:00.000+00:00')")
            store.initialize()
            old_file = store.add_file(BytesIO(b"old"), "旧文件.png", 3, "image/png")
            new_text = store.add_text("保留")
            with store.connect() as db:
                db.execute("UPDATE scratchpad SET created_at = ? WHERE id = ?", ("2025-01-01T00:00:00.000+00:00", old_file["id"]))
            app = create_app(Settings(), root)
            async with api_client(app) as client:
                self.assertIsNone((await client.get("/api/scratchpad/settings")).json()["retention_days"])
                self.assertFalse(store.list_items()[-1]["pinned"])
                self.assertEqual(store.cleanup(), 0)
                response = await client.patch("/api/scratchpad/1/pin", json={"pinned": True})
                self.assertTrue(response.json()["pinned"])
                self.assertEqual((await client.get("/api/scratchpad")).json()[0]["id"], 1)
                self.assertEqual((await client.patch("/api/scratchpad/settings", json={"retention_days": 5})).status_code, 422)
                self.assertEqual((await client.patch("/api/scratchpad/settings", json={"retention_days": 7})).status_code, 200)
                self.assertEqual(Scratchpad(root).retention_days(), 7)
                self.assertEqual(store.cleanup(), 1)
                self.assertEqual([item["id"] for item in store.list_items()], [1, new_text["id"]])
                self.assertEqual(list(store.uploads.iterdir()), [])
                self.assertFalse((await client.patch("/api/scratchpad/1/pin")).json()["pinned"])
                self.assertEqual(store.cleanup(), 1)

    async def test_cleanup_rechecks_pin_and_retention_after_selection(self):
        with TemporaryDirectory() as folder:
            store = Scratchpad(Path(folder))
            store.initialize()
            item = store.add_text("a")
            store.set_retention(3)
            now = datetime.now(timezone.utc) + timedelta(days=4)
            original_delete = store._delete

            def changed(*args):
                store.set_pin(item["id"], True)
                return original_delete(*args)

            with patch.object(store, "_delete", side_effect=changed):
                self.assertEqual(store.cleanup(now), 0)
            store.set_pin(item["id"], False)

            def disabled(*args):
                store.set_retention(None)
                return original_delete(*args)

            with patch.object(store, "_delete", side_effect=disabled):
                self.assertEqual(store.cleanup(now), 0)
            self.assertEqual(store.list_items()[0]["id"], item["id"])


def forecast_fixture():
    day = int(datetime(2026, 1, 3, 5, tzinfo=timezone.utc).timestamp())
    hours = [day + i * 3600 for i in range(48)]
    return {"timezone": "America/Toronto", "current": {"time": hours[3] + 900, "temperature_2m": -3, "uv_index": 2.1},
            "hourly": {"time": hours, "temperature_2m": [2] * 48, "precipitation_probability": list(range(48))},
            "daily": {"time": [day], "weather_code": [0], "temperature_2m_max": [5], "temperature_2m_min": [-4],
                      "sunrise": [hours[7]], "sunset": [hours[17]]}}


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_cache_stale_and_recovery(self):
        calls = []
        fail = False

        def upstream(request):
            calls.append(request.url.path)
            if request.url.path.endswith("air-quality"):
                return httpx.Response(200, json={"current": {"time": 123, "us_aqi": 20}})
            return httpx.Response(503) if fail else httpx.Response(200, json=forecast_fixture())

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            service = Weather(WeatherConfig())
            with patch("backend.weather.monotonic", return_value=5) as clock:
                result = await service.summary(client)
                self.assertEqual(result["status"], "live")
                self.assertEqual(result["data"]["daily"][0]["date"], "2026-01-03")
                self.assertEqual(len(result["data"]["hourly"]), 24)
                data = result["data"]
                self.assertEqual(data["hourly_start"], data["current"]["time"] - 900)
                self.assertEqual(data["hourly"][0]["time"], data["hourly_start"])
                self.assertEqual(data["hourly"][0]["precipitation_percent"], 3)
                self.assertEqual(data["hourly"][-1]["time"] + 3600, data["hourly_end"])
                self.assertEqual(result["data"]["current"]["precipitation_percent"], 3)
                self.assertEqual(result["data"]["current"]["high_c"], 5)
                self.assertEqual(result["data"]["current"]["uv_index"], 2.1)
                self.assertEqual(data["current"]["sunrise"], data["daily"][0]["sunrise"])
                self.assertEqual(data["current"]["sunset"] - data["current"]["sunrise"], 10 * 3600)
                await service.summary(client)
                self.assertEqual(len(calls), 2)
                fail = True
                clock.return_value += FRESH_SECONDS + 1
                stale = await service.summary(client)
                self.assertEqual(stale["status"], "stale")
                self.assertEqual(stale["data"], result["data"])
                self.assertEqual(stale["air_quality"]["status"], "live")
                await service.summary(client)
                self.assertEqual(len(calls), 4)
                clock.return_value += RETRY_SECONDS + 1
                fail = False
                self.assertEqual((await service.summary(client))["status"], "live")

    async def test_first_error_does_not_require_cache(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as client:
            result = await Weather(WeatherConfig()).summary(client)
        self.assertIsNone(result["data"])
        self.assertEqual(result["status"], "unavailable")


def health_fixture():
    first = "2026-01-03 07:00:00 -0500"
    second = "2026-01-03T12:30:00Z"

    def metric(name, units, *data):
        return {"name": name, "units": units, "data": list(data)}

    return {"data": {"metrics": [
        metric("step_count", "count", {"date": first, "qty": 200}, {"date": "2026-01-03T12:00:00Z", "qty": 300},
               {"date": second, "qty": 250}, {"date": "2026-01-02T23:58:00-05:00", "qty": 150}),
        metric("active_energy", "kJ", {"date": first, "qty": 418.4}),
        metric("apple_exercise_time", "min", {"date": first, "qty": 15}),
        metric("walking_running_distance", "mi", {"date": first, "qty": 2}),
        metric("heart_rate", "count/min", {"date": first, "Min": 60, "Avg": 70, "Max": 80},
               {"date": second, "Min": 75, "Avg": 90, "Max": 103}),
        metric("resting_heart_rate", "count/min", {"date": first, "qty": 61}),
        metric("heart_rate_variability_sdnn", "ms", {"date": first, "qty": 46}),
        metric("blood_oxygen_saturation", "%", {"date": first, "qty": .98}),
        metric("respiratory_rate", "count/min", {"date": first, "qty": 15}),
        metric("sleep_analysis", "hr", {"date": first, "sleepStart": "2026-01-02 23:00:00 -0500",
                "sleepEnd": first, "totalSleep": 12, "inBed": 6, "core": 5, "rem": 4, "deep": 3, "awake": 1}),
    ]}}


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_trend_windows_keep_previous_evening_and_missing_calendar_dates(self):
        with TemporaryDirectory() as folder:
            database = Scratchpad(Path(folder))
            database.initialize()
            config = HealthConfig(timezone="America/Toronto")
            health = HealthStore(database.connect, config)
            health.initialize()
            now = datetime(2026, 1, 3, 16, 30, tzinfo=timezone.utc)
            rows = [
                ("heart_rate", "count/min", [
                    {"date": "2026-01-02T16:29:59Z", "qty": 99}, {"date": "2026-01-02T16:31:00Z", "Min": 59, "Avg": 64, "Max": 75},
                    {"date": "2026-01-03T02:15:00Z", "qty": 70}, {"date": "2026-01-03T16:30:01Z", "qty": 104},
                ]),
                ("blood_oxygen_saturation", "%", [
                    {"date": "2025-12-24T16:00:00Z", "qty": 96}, {"date": "2026-01-02T16:00:00Z", "qty": 97},
                    {"date": "2026-01-02T18:00:00Z", "qty": 99},
                ]),
                ("respiratory_rate", "count/min", [{"date": "2026-01-02T16:00:00Z", "qty": 15}]),
            ]
            health.ingest({"data": {"metrics": [{"name": name, "units": unit, "data": samples}
                                                for name, unit, samples in rows]}}, 2)
            recent = health.trend("heart", "24h", now)
            self.assertEqual(recent["start"], "2026-01-02T16:30:00.000000+00:00")
            self.assertEqual(recent["points"][0]["time"], recent["start"])
            self.assertEqual(len(recent["points"]), 2)
            self.assertEqual(recent["stats"], {"min": 59, "avg": 67, "max": 75})
            weekly = health.trend("heart", "7d", now)
            self.assertEqual(len(weekly["points"]), 7)
            self.assertEqual(weekly["points"][-2]["max"], 99)
            self.assertIsNone(weekly["points"][-1]["avg"])
            week = health.trend("oxygen", "7d", now)
            month = health.trend("oxygen", "30d", now)
            self.assertEqual(week["points"][-2], {"date": "2026-01-02", "avg": 98})
            self.assertEqual(week["stats"], {"latest": 99, "avg": 98})
            self.assertEqual(len(month["points"]), 30)
            self.assertIn({"date": "2025-12-24", "avg": 96}, month["points"])
            self.assertEqual(health.trend("respiratory", "7d", now)["stats"]["latest"], 15)
            async with api_client(create_app(Settings(health=config), Path(folder))) as client:
                self.assertEqual((await client.get("/api/health/trend/oxygen?period=7d")).status_code, 200)
                self.assertEqual((await client.get("/api/health/trend/oxygen?period=24h")).status_code, 422)
                self.assertEqual((await client.get("/api/health/trend/unknown?period=7d")).status_code, 404)

    async def test_dual_ingest_normalizes_units_and_sleep_and_rejects_bad_payload(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            export = root / "exports"
            export.mkdir()
            config = HealthConfig(export_dir=export, ingest_token="test-token", timezone="America/Toronto")
            database = Scratchpad(root / "data")
            database.initialize()
            health = HealthStore(database.connect, config)
            health.initialize()
            fixture = health_fixture()
            self.assertFalse(health.summary()["available"])
            local_copy = export / "HealthAutoExport-1.json"
            local_copy.write_text(json.dumps(fixture), encoding="utf-8")
            health.scan()
            now = datetime(2026, 1, 3, 16, tzinfo=timezone.utc)
            initial = health.summary(now)
            self.assertEqual(initial["totals"]["step_count"], 550)
            self.assertAlmostEqual(initial["totals"]["active_energy"], 100)
            self.assertAlmostEqual(initial["totals"]["walking_running_distance"], 3.218688)
            self.assertEqual(initial["heart_rate"]["today"], {"min": 60, "avg": 80, "max": 103})
            self.assertEqual(len(initial["heart_rate"]["hourly"]), 1)
            self.assertEqual(initial["latest"]["heart_rate_variability"], 46)
            self.assertEqual(initial["latest"]["blood_oxygen_saturation"], 98)
            self.assertEqual(initial["sleep"]["total_minutes"], 300)
            self.assertAlmostEqual(sum(initial["sleep"]["stages"][key] for key in ("deep", "rem", "core")), 300)
            self.assertTrue(initial["sleep"]["adjusted"])
            settings = Settings(health=config)
            async with api_client(create_app(settings, root / "data")) as client:
                self.assertEqual((await client.post("/api/health/ingest", json=fixture)).status_code, 401)
                headers = {"Authorization": "Bearer test-token"}
                self.assertEqual((await client.post("/api/health/ingest", json={}, headers=headers)).status_code, 422)
                fixture["data"]["metrics"][0]["data"][2]["qty"] = 500
                self.assertEqual((await client.post("/api/health/ingest", json=fixture, headers=headers)).status_code, 200)
                self.assertEqual((await client.post("/api/health/ingest", json=fixture, headers=headers)).json()["imported"], 0)
                self.assertEqual((await client.get("/api/health/detail/activity")).status_code, 200)
                self.assertEqual((await client.get("/api/health/detail/unknown")).status_code, 404)
            self.assertEqual(health.summary(now)["totals"]["step_count"], 800)
            restarted = HealthStore(database.connect, config)
            restarted.initialize()
            restarted.scan()
            self.assertEqual(restarted.summary(now)["totals"]["step_count"], 800)
            fixture["data"]["metrics"][0]["data"][2]["qty"] = 12
            partial = {"data": {"metrics": [{**fixture["data"]["metrics"][0],
                       "data": [fixture["data"]["metrics"][0]["data"][2]]}]}}
            (export / "HealthAutoExport-2.json").write_text(json.dumps(partial), encoding="utf-8")
            restarted.scan()
            self.assertEqual(restarted.summary(now)["totals"]["step_count"], 312)
            fixture["data"]["metrics"][0]["data"][2]["qty"] = 42
            self.assertGreater(restarted.ingest(fixture, priority=2), 0)
            self.assertEqual(restarted.summary(now)["totals"]["step_count"], 342)

    async def test_detail_rollups_convert_units_and_preserve_missing_intervals(self):
        with TemporaryDirectory() as folder:
            database = Scratchpad(Path(folder))
            database.initialize()
            health = HealthStore(database.connect, HealthConfig(timezone="America/Toronto"))
            health.initialize()
            now = datetime(2026, 1, 3, 16, tzinfo=timezone.utc)
            empty = health.detail("steps", now)
            self.assertEqual(len(empty["weekly"]), 7)
            self.assertIsNone(empty["values"]["step_count"])
            fixture = health_fixture()
            for name, unit, value in [
                ("basal_energy_burned", "kJ", 836.8), ("apple_stand_time", "seconds", 240),
                ("flights_climbed", "count", 5), ("walking_speed", "mi/hr", 3),
                ("walking_step_length", "in", 25), ("walking_heart_rate_average", "count/min", 88),
            ]:
                fixture["data"]["metrics"].append({"name": name, "units": unit,
                                                   "data": [{"date": "2026-01-03T12:00:00Z", "qty": value}]})
            fixture["data"]["metrics"].append({"name": "apple_stand_hour", "data": [
                {"date": "2026-01-03T12:00:00Z", "value": "Stood"}, {"date": "2026-01-03T13:00:00Z", "value": "Idle"}]})
            health.ingest(fixture, priority=2)
            steps = health.detail("steps", now)
            self.assertEqual(steps["hourly"][0]["step_count"], 550)
            self.assertEqual(steps["weekly"][-2]["step_count"], 150)
            self.assertIsNone(steps["weekly"][-3]["step_count"])
            self.assertAlmostEqual(steps["values"]["walking_speed"], 4.828032)
            self.assertEqual(steps["values"]["walking_step_length"], 63.5)
            self.assertEqual(steps["values"]["flights_climbed"], 5)
            activity = health.detail("activity", now)
            self.assertAlmostEqual(activity["weekly"][-1]["basal_energy_burned"], 200)
            self.assertEqual(activity["values"]["apple_stand_hour"], 1)
            self.assertEqual(activity["values"]["apple_stand_time"], 4)
            heart = health.detail("heart", now)
            self.assertEqual(heart["values"]["walking_heart_rate_average"], 88)
            self.assertEqual(heart["weekly"][-1]["resting_heart_rate"], 61)
            self.assertEqual(heart["hourly"][0]["avg"], 80)
            self.assertEqual(heart["hrv"][0]["value"], 46)
            sleep = health.detail("sleep", now)
            self.assertAlmostEqual(sleep["sleep"]["efficiency_percent"], 100 * 300 / 360)
            self.assertAlmostEqual(sum(v for v in sleep["sleep"]["stage_percent"].values() if v is not None), 100)
            self.assertEqual(sleep["weekly"][-1]["total_minutes"], 300)
            self.assertEqual(sleep["values"], {"blood_oxygen_saturation": 98, "respiratory_rate": 15})

    async def test_raw_sleep_dedupes_overlapping_segments(self):
        with TemporaryDirectory() as folder:
            database = Scratchpad(Path(folder))
            database.initialize()
            health = HealthStore(database.connect, HealthConfig())
            health.initialize()
            rows = [
                {"start": "2026-01-02T22:00:00-05:00", "end": "2026-01-03T06:00:00-05:00", "value": "In Bed"},
                {"start": "2026-01-02T23:00:00-05:00", "end": "2026-01-03T01:00:00-05:00", "value": "Core"},
                {"start": "2026-01-03T00:00:00-05:00", "end": "2026-01-03T02:00:00-05:00", "value": "REM"},
            ]
            health.ingest({"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": rows}]}}, 2)
            sleep = health.summary()["sleep"]
            self.assertEqual(sleep["total_minutes"], 180)
            self.assertEqual(sleep["in_bed_minutes"], 480)
            self.assertEqual(sleep["stages"]["rem"], 120)


class AiTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_and_provider_failure_preserve_text(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Scratchpad(root)
            store.initialize()
            note = store.add_text("keep this text")
            async with api_client(create_app(Settings(ai={"enabled": True}), root)) as client:
                self.assertEqual((await client.get("/api/ai/status")).json(), {"enabled": False})
                self.assertEqual((await client.post("/api/ai/analyze", json={"id": note["id"]})).status_code, 503)
            app = create_app(Settings(ai={"enabled": True, "api_key": "test-key"}), root)

            def upstream(request):
                self.assertEqual(request.url.path, "/v1/chat/completions")
                self.assertEqual(json.loads(request.content)["messages"][-1]["content"], "keep this text")
                return httpx.Response(429, text="provider detail test-key")

            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external, api_client(app) as client:
                app.state.external_client = external
                failure = await client.post("/api/ai/analyze", json={"id": note["id"]})
                self.assertEqual(failure.status_code, 502)
                self.assertNotIn("test-key", failure.text)
                self.assertEqual(store.get_text(note["id"]), "keep this text")

    async def test_commands_do_not_delegate_shell_or_provider(self):
        control = SimpleNamespace(settings=Settings(), set_power=Mock(return_value={"fan_control": {}}),
                                  schedule_shutdown=Mock(return_value="2026-01-03T13:00:00Z"), cancel_shutdown=Mock())
        await execute_command(control, "切到静音模式")
        control.set_power.assert_called_once_with("silent")
        self.assertEqual((await execute_command(control, "30分钟后关机"))["kind"], "shutdown")
        control.schedule_shutdown.assert_called_once_with(30)
        await execute_command(control, "取消关机")
        control.cancel_shutdown.assert_called_once()
        for rejected in ("关机", "切到静音模式 & shutdown.exe", "执行任意 cmd", "0分钟后关机时再执行脚本"):
            with self.assertRaises(HTTPException):
                await execute_command(control, rejected)

    async def test_shutdown_queue_can_be_cancelled_without_calling_windows(self):
        controller = PowerControl(Settings())
        with patch("backend.control.os.name", "nt"), patch("backend.control.shutdown_windows") as command:
            controller.schedule_shutdown(30)
            with self.assertRaises(HTTPException):
                controller.schedule_shutdown(20)
            await controller.close()
            self.assertIsNone(controller.shutdown_at)
            command.assert_not_called()
