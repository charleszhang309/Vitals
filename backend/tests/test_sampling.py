import asyncio
from contextlib import suppress
import json
from unittest.mock import patch

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


@pytest.mark.parametrize(("mode", "unobserved", "observed"), [
    ("smart", 5, 1), ("realtime", 1, 1), ("eco", 5, 5),
])
def test_sampling_intervals_follow_mode_and_first_and_last_subscribers(mode, unobserved, observed):
    monitor = Monitor(Settings(sampling_mode=mode), {"platform": "windows", "name": "test"})

    assert monitor.sampling_interval == unobserved
    first = monitor.subscribe()
    assert monitor.sampling_interval == observed
    second = monitor.subscribe()
    monitor.unsubscribe(first)
    assert monitor.sampling_interval == observed
    monitor.unsubscribe(second)
    assert monitor.sampling_interval == unobserved


@pytest.mark.parametrize("change", ["subscriber", "mode"])
def test_shorter_sampling_interval_wakes_pending_monitor(change):
    async def check():
        monitor = Monitor(Settings(), {"platform": "windows", "name": "test"})
        published = asyncio.Queue()

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
                    assert idle_sample["sampling_interval_seconds"] == 5
                    clock.return_value = 2
                    if change == "subscriber":
                        monitor.subscribe()
                    else:
                        monitor.set_sampling_mode("realtime")
                    active_sample = await asyncio.wait_for(published.get(), timeout=2)
                    assert active_sample["sampling_interval_seconds"] == 1
                    clock.return_value = 2.25
                    monitor.set_sampling_mode("eco")
                    monitor.set_sampling_mode("realtime")
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(published.get(), timeout=0.05)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    asyncio.run(check())


def test_five_second_samples_keep_temperature_and_task_thresholds_running():
    rules = Rules(Settings(task={"high_seconds": 10, "low_seconds": 10}, alerts={"temperature_seconds": 10}))

    def tick(now, load):
        sample = {"timestamp": str(now), "cpu": {"usage_percent": load}, "gpus": [], "disks": []}
        return [event["type"] for event in rules.evaluate(sample, {"CPU": 95}, now)]

    assert tick(0, 95) == []
    assert tick(5, 95) == []
    assert tick(10, 95) == ["cpu_temperature"]
    assert rules.job["state"] == "running"
    assert tick(15, 2) == []
    assert tick(20, 2) == []
    assert tick(25, 2) == ["task_completed"]
