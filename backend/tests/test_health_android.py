import asyncio
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import httpx

from backend.config import HealthConfig, Settings
from backend.health import HealthStore, parse_export
from backend.main import create_app
from backend.scratchpad import Scratchpad


UTC = ZoneInfo("UTC")
DAY = "2026-11-25"
NOW = datetime(2026, 11, 25, 20, tzinfo=timezone.utc)


def init_store(tmp_path, config=None):
    scratchpad = Scratchpad(tmp_path)
    scratchpad.initialize()
    store = HealthStore(scratchpad.connect, config or HealthConfig())
    store.initialize()
    return store


def test_hcgateway_wrapper_records_share_apple_sqlite_rows(tmp_path):
    sleep_start, sleep_end = "2026-11-24T23:00:00Z", f"{DAY}T06:00:00Z"
    gateway = {"data": {
        "Steps": [{"id": "fixture-id-1", "start": f"{DAY}T13:00:00Z", "end": f"{DAY}T14:00:00Z", "data": {"count": 420}}],
        "HeartRate": [{"start": f"{DAY}T13:00:00Z", "end": f"{DAY}T13:10:00Z", "data": {"samples": [
            {"time": f"{DAY}T13:02:00Z", "beatsPerMinute": 68}, {"time": f"{DAY}T13:04:00Z", "beatsPerMinute": 76},
        ]}}],
        "SleepSession": [{"start": sleep_start, "end": sleep_end, "data": {"stages": [
            {"stage": 5, "startTime": sleep_start, "endTime": f"{DAY}T01:00:00Z"},
            {"stage": "STAGE_TYPE_LIGHT", "startTime": f"{DAY}T01:00:00Z", "endTime": f"{DAY}T04:00:00Z"},
            {"stage": 6, "startTime": f"{DAY}T04:00:00Z", "endTime": f"{DAY}T05:45:00Z"},
            {"stage": 7, "startTime": f"{DAY}T05:45:00Z", "endTime": sleep_end},
        ]}}],
        "ActiveCaloriesBurned": [{"start": f"{DAY}T13:00:00Z", "data": {"energy": {"inKilojoules": 418.4}}}],
        "TotalCaloriesBurned": [{"start": f"{DAY}T13:00:00Z", "data": {"energy": {"inKilocalories": 600}}}],
        "oxygen_saturation": [{"start": f"{DAY}T13:01:00Z", "data": {"percentage": 97}}],
    }}
    store = init_store(tmp_path)
    assert store.ingest(gateway, 2) == 10
    assert store.ingest(gateway, 1) == 0
    assert store.ingest(gateway, 2) == 0
    overview = store.summary(NOW)
    assert overview["totals"]["step_count"] == 420
    assert abs(overview["totals"]["active_energy"] - 100) < 0.0001
    assert overview["latest"]["blood_oxygen_saturation"] == 97
    assert overview["heart_rate"]["today"] == {"min": 68, "max": 76, "avg": 72}
    assert overview["sleep"]["total_minutes"] == 405
    assert overview["sleep"]["stages"] == {"deep": 120, "rem": 105, "core": 180, "awake": 15, "asleep": None}
    assert store.detail("activity", NOW)["values"]["total_energy_burned"] == 600
    apple = {"data": {"metrics": [{"name": "step_count", "data": [{"date": f"{DAY} 08:00:00 -0500", "qty": 420}]}]}}
    assert store.ingest(apple, 1) == 0
    assert store.summary(NOW)["totals"]["step_count"] == 420


def test_tasker_ingest_http_token_bare_array_and_updated_value(tmp_path):
    settings = Settings(health=HealthConfig(ingest_token="fixture-token"))
    store = init_store(tmp_path, settings.health)
    app = create_app(settings, tmp_path, static_dir=tmp_path / "no-static")
    tasker = {"metrics": [{"type": "steps", "value": 8500, "start": f"{DAY}T08:00:00Z", "end": f"{DAY}T18:00:00Z"}]}

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            path = "/api/health/ingest"
            async def post(data):
                return await client.post(path, headers={"Authorization": "Bearer fixture-token"}, json=data)

            assert (await client.post(path, json=tasker)).status_code == 401
            assert (await post(tasker)).json() == {"imported": 1}
            assert (await post(tasker["metrics"])).json() == {"imported": 0}
            tasker["metrics"][0]["value"] = 9000
            assert (await post(tasker)).json() == {"imported": 1}
            assert (await post({"type": "SleepSessionRecord", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T06:30:00Z"})).json() == {"imported": 1}
            assert (await post({"unrelated": {"name": "Example"}})).status_code == 422

    asyncio.run(verify())
    overview = store.summary(NOW)
    assert overview["totals"]["step_count"] == 9000
    assert overview["sleep"]["total_minutes"] == 390


def test_health_sync_csv_and_json_scan_subfolders_and_deduplicate(tmp_path):
    export = tmp_path / "Sync"
    (export / "Daily").mkdir(parents=True)
    (export / "History").mkdir()
    path = export / "Daily" / "Steps-and-Heart CSV.csv"
    (export / "History" / "Steps-copy.csv").write_text(f"Date;Start Time;Steps\n{DAY};08:00;1,250\n", encoding="utf-8")
    path.write_text(f"Date;Start Time;Steps;Heart Rate (bpm);Active Calories (kcal)\n{DAY};08:00;1,250;72;38,5\n", encoding="utf-8-sig")
    (export / "Daily" / "Sleep-week.csv").write_text(
        f"Start Date,Start Time,End Date,End Time,Sleep Duration (minutes),Deep Sleep (minutes),REM (minutes)\n"
        f"2026-11-24,23:00,{DAY},07:00,450,80,100\n", encoding="utf-16")
    (export / "History" / "Heart Rate 2026-11-25.json").write_text(json.dumps({"records": [
        {"type": "heart_rate", "start": f"{DAY}T08:00:00Z", "value": 72},
    ]}), encoding="utf-8")
    store = init_store(tmp_path, HealthConfig(export_dir=export))
    store.scan()
    first = store.summary(NOW)
    assert first["totals"]["step_count"] == 1250
    assert first["totals"]["active_energy"] == 38.5
    assert first["heart_rate"]["current"] == 72
    assert first["sleep"]["total_minutes"] == 450 and first["sleep"]["stages"]["deep"] == 80
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM health_samples").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM health_file_imports").fetchone()[0] == 4
    store.scan()
    assert store.summary(NOW) == first
    path.write_text(f"Date;Start Time;Steps;Heart Rate (bpm);Active Calories (kcal)\n{DAY};08:00;15000;72;38,5\n", encoding="utf-8-sig")
    store.scan()
    assert store.summary(NOW)["totals"]["step_count"] == 15000


def test_common_daily_export_only_imports_detailed_heart_samples():
    body = {"messages": [{"date": DAY, "steps": {"total_steps": 600, "records_count": 5},
        "heart_rate": {"avg_bpm": 88, "records_count": 3, "samples": [{"time": f"{DAY}T09:00:00+02:00", "bpm": 73}]},
        "sleep": {"total_duration_minutes": 400, "sleep_stages": {"Deep sleep": 90, "Light sleep": 200, "REM sleep": 110}},
    }]}
    samples = parse_export(body, UTC)
    beats, = [(at, value) for (name, at), value in samples.items() if name == "heart_rate"]
    assert beats == (f"{DAY}T07:00:00.000000+00:00", {"avg": 73, "min": 73, "max": 73, "count": 1})
    assert samples[("step_count", f"{DAY}T00:00:00.000000+00:00")] == {"qty": 600}
    assert samples[("sleep_analysis", f"{DAY}T00:00:00.000000+00:00")]["total_minutes"] == 400


def test_mixed_metric_envelope_preserves_apple_units():
    at = f"{DAY}T00:00:00.000000+00:00"
    body = {"data": {"metrics": [
        {"name": "walking_running_distance", "units": "mi", "data": [{"date": at, "qty": 1}]},
        {"type": "HeartRate", "data": [{"time": at, "beatsPerMinute": 69}]},
    ]}}
    samples = parse_export(body, UTC)
    assert samples[("walking_running_distance", at)] == {"qty": 1.609344}
    assert samples[("heart_rate", at)]["avg"] == 69
