import asyncio
from contextlib import suppress
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.config import Settings, load_settings
from backend.main import create_app
from backend.monitor import Monitor, Rules


SAMPLING_URL = "/api/system/sampling"


@pytest.fixture
def sampling_app(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ai": {"api_key": "fixture-key"}}), encoding="utf-8")
    app = create_app(data_dir=tmp_path / "data", config_path=path)
    app.state.monitor = Monitor(load_settings(path), {"platform": "windows", "name": "test"})
    return app, path


def test_sampling_preference_updates_live_and_survives_reload(sampling_app):
    app, path = sampling_app
    client = TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 50000))

    assert client.get(SAMPLING_URL).json() == {"mode": "smart"}
    assert client.put(SAMPLING_URL, json={"mode": "eco"}).json() == {"mode": "eco"}
    assert client.get(SAMPLING_URL).json() == {"mode": "eco"}
    assert app.state.monitor.sampling_interval == 5
    assert client.put(SAMPLING_URL, json={"mode": "realtime"}).json() == {"mode": "realtime"}
    assert app.state.monitor.sampling_interval == 1
    assert load_settings(path).sampling_mode == "realtime"
    assert json.loads(path.read_text(encoding="utf-8"))["ai"]["api_key"] == "fixture-key"
    assert not client.get("/api/setup/config").json()["restart_required"]

    assert client.patch("/api/setup/config", json={"weather": {"city_name": "Montréal"}}).status_code == 200
    assert load_settings(path).sampling_mode == "realtime"


@pytest.mark.parametrize("payload", [{"mode": "fast"}, {"mode": 1}, {}, {"mode": "smart", "extra": True}])
def test_sampling_preference_rejects_invalid_updates(sampling_app, payload):
    app, path = sampling_app
    client = TestClient(app, client=("127.0.0.1", 50000))
    before = path.read_bytes()

    assert client.put(SAMPLING_URL, json=payload).status_code == 422
    assert client.get(SAMPLING_URL).json() == {"mode": "smart"}
    assert path.read_bytes() == before


def test_sampling_preference_leaves_running_mode_unchanged_if_write_fails(sampling_app):
    app, path = sampling_app
    client = TestClient(app, client=("127.0.0.1", 50000))
    path.write_text("{{ invalid", encoding="utf-8")

    assert client.put(SAMPLING_URL, json={"mode": "realtime"}).status_code == 409
    assert client.get(SAMPLING_URL).json() == {"mode": "smart"}


def test_concurrent_sampling_and_setup_updates_keep_both_changes(sampling_app):
    app, path = sampling_app

    async def update():
        transport = httpx.ASGITransport(app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:3993") as client:
            results = await asyncio.gather(
                client.put(SAMPLING_URL, json={"mode": "eco"}),
                client.patch("/api/setup/config", json={"ai": {"api_key": "fixture-new-key"}}),
                client.put(SAMPLING_URL, json={"mode": "realtime"}),
            )
            assert [result.status_code for result in results] == [200, 200, 200]
            return (await client.get(SAMPLING_URL)).json()["mode"]

    active_mode = asyncio.run(update())
    settings = load_settings(path)
    assert settings.sampling_mode == active_mode
    assert settings.ai.api_key.get_secret_value() == "fixture-new-key"


def test_sampling_preference_without_config_path_uses_provided_settings(tmp_path):
    app = create_app(Settings(sampling_mode="eco"), data_dir=tmp_path / "data")
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.get(SAMPLING_URL).json() == {"mode": "eco"}
    assert client.put(SAMPLING_URL, json={"mode": "realtime"}).status_code == 409


@pytest.mark.parametrize(("mode", "idle", "ordinary", "active"), [
    ("smart", 15, 5, 1), ("realtime", 1, 1, 1), ("eco", 5, 5, 5),
])
def test_sampling_intervals_follow_mode_and_most_active_subscriber(mode, idle, ordinary, active):
    monitor = Monitor(Settings(sampling_mode=mode), {"platform": "windows", "name": "test"})

    assert monitor.sampling_interval == idle
    first = monitor.subscribe()
    assert monitor.sampling_interval == ordinary
    monitor.set_client_state(first, visible=False, active_telemetry=True)
    assert monitor.sampling_interval == idle
    second = monitor.subscribe()
    assert monitor.sampling_interval == ordinary
    monitor.set_client_state(first, visible=True, active_telemetry=True)
    assert monitor.sampling_interval == active
    monitor.set_client_state(first, visible=True, active_telemetry=False)
    assert monitor.sampling_interval == ordinary
    monitor.set_client_state(first, visible=True, active_telemetry=True)
    monitor.unsubscribe(first)
    assert monitor.sampling_interval == ordinary
    monitor.unsubscribe(second)
    assert monitor.sampling_interval == idle


def test_metrics_receives_valid_client_states_and_ignores_invalid_messages(sampling_app):
    app, _ = sampling_app
    monitor = app.state.monitor
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/ws/metrics")
    messages = [
        (5, {"bytes": b"not text"}),
        (5, {"text": "not JSON"}),
        (5, {"text": '{"type":"client_state","visible":false,"active_telemetry":1}'}),
        (5, {"text": '{"type":"other","visible":false,"active_telemetry":false}'}),
        (5, {"text": '{"type":"client_state","visible":false}'}),
        (5, {"text": '{"type":"client_state","visible":false,"active_telemetry":false}'}),
        (15, {"text": '{"type":"client_state","visible":true,"active_telemetry":true}'}),
        (1, {"text": '{"type":"client_state","visible":true,"active_telemetry":false}'}),
    ]

    async def receive():
        if not messages:
            assert monitor.sampling_interval == 5
            return {"type": "websocket.disconnect"}
        interval, message = messages.pop(0)
        assert monitor.sampling_interval == interval
        return {"type": "websocket.receive", **message}

    asyncio.run(endpoint(SimpleNamespace(app=app, accept=AsyncMock(), send_json=AsyncMock(), receive=receive)))
    assert not monitor.subscribers
    assert monitor.sampling_interval == 15


@pytest.mark.parametrize(("change", "interval"), [("subscriber", 5), ("active", 1), ("mode", 1)])
def test_shorter_sampling_interval_wakes_pending_monitor(change, interval):
    async def check():
        monitor = Monitor(Settings(), {"platform": "windows", "name": "test"})
        published = asyncio.Queue()
        if change == "active":
            mailbox = monitor.subscribe()
            monitor.set_client_state(mailbox, visible=False, active_telemetry=False)

        def sample():
            return {"cpu": {"usage_percent": 50}, "memory": {}, "disks": []}

        async def read_lhm(_client):
            return None

        with patch("backend.monitor.monotonic", return_value=0) as clock, \
                patch.object(monitor.system, "sample", side_effect=sample), \
                patch.object(monitor, "read_lhm", side_effect=read_lhm), \
                patch.object(monitor, "publish", side_effect=published.put_nowait):
            async with httpx.AsyncClient() as client:
                task = asyncio.create_task(monitor.run(client))
                try:
                    idle_sample = await asyncio.wait_for(published.get(), timeout=2)
                    assert idle_sample["sampling_interval_seconds"] == 15
                    clock.return_value = 6
                    if change == "mode":
                        monitor.set_sampling_mode("realtime")
                    elif change == "active":
                        monitor.set_client_state(mailbox, visible=True, active_telemetry=True)
                    else:
                        monitor.subscribe()
                    active_sample = await asyncio.wait_for(published.get(), timeout=2)
                    assert active_sample["sampling_interval_seconds"] == interval
                    clock.return_value = 6.25
                    monitor.set_sampling_mode("eco")
                    monitor.set_sampling_mode("realtime")
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(published.get(), timeout=0.05)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    asyncio.run(check())


@pytest.mark.parametrize("interval", [5, 15])
def test_scheduled_samples_keep_temperature_and_task_thresholds_running(interval):
    rules = Rules(Settings(task={"high_seconds": 10, "low_seconds": 10}, alerts={"temperature_seconds": 10}))

    def tick(now, load, temp):
        sample = {"timestamp": str(now), "cpu": {"usage_percent": load}, "gpus": [], "disks": []}
        return [event["type"] for event in rules.evaluate(sample, {"CPU": temp}, now, expected_interval=interval)]

    steady = range(0, 10, interval)
    for now in steady:
        assert tick(now, 95, 95) == []
    next_sample = steady[-1] + interval
    assert tick(next_sample, 95, 95) == ["cpu_temperature"]
    assert rules.job["state"] == "running"
    low_start = next_sample + interval
    for step in steady:
        assert tick(low_start + step, 2, 30) == []
    assert tick(low_start + next_sample, 2, 30) == ["task_completed"]


def test_idle_sampling_still_resets_thresholds_on_missed_samples():
    rules = Rules(Settings(task={"high_seconds": 10}, alerts={"temperature_seconds": 10}))
    sample = {"timestamp": "time", "cpu": {"usage_percent": 95}, "gpus": [], "disks": []}

    assert rules.evaluate(sample, {"CPU": 95}, 0, expected_interval=15) == []
    assert rules.evaluate(sample, {"CPU": 95}, 30, expected_interval=15) == []
    assert rules.job["state"] == "idle"
