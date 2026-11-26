import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx

from backend.config import Settings
from backend.main import create_app
from backend.rate_history import HOUR, RateHistory
from backend.rates import Rates
from backend.scratchpad import Scratchpad


def test_rate_history_ranges_cache_and_stale(tmp_path, monkeypatch):
    app = create_app(Settings(), tmp_path)
    clock = 100
    calls = []
    offline = False
    monkeypatch.setattr("backend.rate_history.monotonic", lambda: clock)

    def provider(request):
        params = request.url.params
        calls.append((params["base"], params["quotes"], params["from"], params["to"]))
        if offline:
            return httpx.Response(503)
        return httpx.Response(200, json=[
            {"base": "CAD", "quote": "CNY", "date": params["to"], "rate": 5.25},
            {"base": "CAD", "quote": "CNY", "date": params["from"], "rate": 5.10},
            {"base": "CAD", "quote": "USD", "date": params["from"], "rate": 0.75},
        ])

    async def verify():
        nonlocal clock, offline
        async with (httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client):
            app.state.external_client = upstream
            url = "/api/rates/history?base=CAD&target=CNY&range="
            for window, days in (("7d", 7), ("30d", 30), ("1y", 365)):
                first, second = await asyncio.gather(client.get(url + window), client.get(url + window))
                result = first.raise_for_status().json()
                assert second.json() == result
                assert result["status"] == "live" and result["granularity"] == "daily"
                assert [point["rate"] for point in result["points"]] == [5.10, 5.25]
                base, target, start, end = calls[-1]
                assert (base, target) == ("CAD", "CNY")
                assert (date.fromisoformat(end) - date.fromisoformat(start)).days == days - 1
            assert len(calls) == 3
            assert (await client.get(url + "5d")).status_code == 422
            assert (await client.get("/api/rates/history?base=INVALID&target=CNY")).status_code == 422
            clock += HOUR + 1
            offline = True
            stale = (await client.get(url + "7d")).json()
            assert stale["status"] == "stale" and len(stale["points"]) == 2
            assert (await client.get(url + "7d")).json() == stale
            assert len(calls) == 4

    asyncio.run(verify())


def test_24_hours_uses_recent_daily_quote_then_local_snapshots(tmp_path, monkeypatch):
    settings = Settings()
    app = create_app(settings, tmp_path)
    store = Scratchpad(tmp_path)
    store.initialize()
    history = RateHistory(Rates(settings.finance), store.connect)
    history.initialize()
    now = 1_800_000_000 // HOUR * HOUR
    monkeypatch.setattr("backend.rate_history.time", lambda: now)
    quote = {"base": "CAD", "target": "CNY", "rate": 5.27, "status": "live"}
    quote_date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    provider = httpx.MockTransport(lambda _: httpx.Response(200, json=[
        {"base": "CAD", "quote": "CNY", "rate": 5.27, "date": quote_date},
    ]))

    async def verify():
        async with (httpx.AsyncClient(transport=provider) as upstream,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client):
            app.state.external_client = upstream
            url = "/api/rates/history?base=CAD&target=CNY&range=24h"
            fallback = (await client.get(url)).json()
            assert fallback["granularity"] == "daily" and fallback["points"] == [{"time": quote_date, "rate": 5.27}]
            history.store(now - 25 * HOUR, [quote])
            history.store(now - 2 * HOUR, [quote])
            history.store(now - HOUR, [{**quote, "status": "stale"}])
            history.store(now, [{**quote, "rate": 5.30}])
            history.store(now, [{**quote, "rate": 5.40}])
            snapshots = (await client.get(url)).json()
            assert snapshots["granularity"] == "hourly_snapshot"
            assert [point["rate"] for point in snapshots["points"]] == [5.27, 5.30]
            assert all(datetime.fromisoformat(point["time"]).tzinfo for point in snapshots["points"])

    asyncio.run(verify())


def test_immediate_and_custom_shutdown_require_confirmation_and_can_be_cancelled(tmp_path):
    with patch("backend.main.host_identity", return_value={"platform": "windows", "name": "Fixture"}):
        app = create_app(Settings(), tmp_path)

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            path = "/api/control/shutdown"
            with patch("backend.control.os.name", "nt"), patch("backend.control.shutdown_windows") as command:
                for minutes in (0, 1441, 2.5, True, "15"):
                    assert (await client.post(path, json={"minutes": minutes, "confirmed": True})).status_code == 422
                for minutes in (1, 17, 1440):
                    assert (await client.post(path, json={"minutes": minutes})).status_code == 409
                    before = datetime.now(timezone.utc)
                    scheduled = (await client.post(path, json={"minutes": minutes, "confirmed": True})).raise_for_status().json()
                    delay = (datetime.fromisoformat(scheduled["shutdown_at"]) - before).total_seconds()
                    assert minutes * 60 - 1 <= delay <= minutes * 60 + 1
                    assert (await client.get("/api/control/quick")).json() == scheduled
                    assert (await client.post(path, json={"minutes": 1, "confirmed": True})).status_code == 409
                    assert (await client.delete(path)).json() == {"shutdown_at": None}
                    assert (await client.get("/api/control/quick")).json() == {"shutdown_at": None}
                command.assert_not_called()

    asyncio.run(verify())
