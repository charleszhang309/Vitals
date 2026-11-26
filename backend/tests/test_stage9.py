import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.config import Settings
from backend.life import LifeStore
from backend.main import create_app
from backend.scratchpad import Scratchpad


AUTH = {"Authorization": "Bearer fixture-agent-token"}
AGENT = "/api/agents/macbook"
SYNC = "/api/todos/sync/reminders"
SNAPSHOT = {
    "name": "MacBook", "os": "macos", "timestamp": "2026-01-01T00:00:00Z",
    "battery": {"percent": 63, "charging": False, "state": "discharging", "minutes_left": 95},
    "cpu": {"usage_percent": 12}, "memory": {"used_bytes": 4, "total_bytes": 8, "percent": 50},
    "disks": [{"mountpoint": "/", "free_bytes": 50, "total_bytes": 100}],
    "network": {"download_bytes_per_sec": 100, "upload_bytes_per_sec": 50},
    "top_processes": [{"name": f"process-{i}", "rss_bytes": i} for i in range(8)],
    "volume": {"percent": 47, "muted": False},
    "media": {"title": "Fixture track", "artist": "Fixture artist", "status": "paused", "player": "Music"},
}


def client_for(tmp_path, token="fixture-agent-token"):
    settings = Settings(agents={"token": token}, health={"timezone": "America/Toronto"})
    app = create_app(settings, tmp_path, static_dir=tmp_path / "no-static")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def test_heartbeat_authorization_and_server_offline_clock(tmp_path, monkeypatch):
    now = 100
    monkeypatch.setattr("backend.agents.monotonic", lambda: now)

    async def verify():
        nonlocal now
        async with client_for(tmp_path, token=None) as client:
            assert (await client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).status_code == 503
        async with client_for(tmp_path) as client:
            assert (await client.get("/api/agents")).json() == {"devices": []}
            for headers in ({}, {"Authorization": "Basic fixture-agent-token"}, {"Authorization": "Bearer wrong"}):
                rejected = await client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=headers)
                assert rejected.status_code == 401
            assert (await client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).json() == {"commands": []}
            info = (await client.get("/api/agents")).json()["devices"][0]
            assert info["device_id"] == "macbook" and info["online"]
            assert info["snapshot"]["battery"]["state"] == "discharging"
            assert info["last_seen"] > info["snapshot"]["timestamp"]
            assert [row["rss_bytes"] for row in info["snapshot"]["top_processes"]] == [7, 6, 5, 4, 3]
            now += 15
            assert (await client.get("/api/agents")).json()["devices"][0]["online"]
            now += 0.001
            response = await client.get("/api/agents")
            assert response.headers["cache-control"] == "no-store"
            assert not response.json()["devices"][0]["online"]
            assert (await client.post(AGENT + "/command", json={"action": "lock"})).status_code == 409
            assert (await client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).status_code == 200
            assert (await client.get("/api/agents")).json()["devices"][0]["online"]

    asyncio.run(verify())


def test_command_whitelist_delivery_and_result(tmp_path, monkeypatch):
    now = 100
    monkeypatch.setattr("backend.agents.monotonic", lambda: now)
    commands = [
        {"action": "volume_set", "payload": {"percent": 45}},
        {"action": "volume_mute", "payload": {"muted": True}},
        *({"action": action, "payload": {}} for action in
          ("media_play_pause", "media_next", "media_previous", "lock", "display_sleep", "sleep")),
    ]

    async def verify():
        nonlocal now
        async with client_for(tmp_path) as client:
            for path in (AGENT, "/api/agents/other"):
                assert (await client.post(path + "/heartbeat", json=SNAPSHOT, headers=AUTH)).status_code == 200
            for payload in (
                {"action": "shell"}, {"action": "lock", "payload": {"extra": "value"}},
                {"action": "volume_set", "payload": {"percent": 101}},
                {"action": "volume_set", "payload": {"percent": True}},
                {"action": "volume_mute", "payload": {"muted": "true"}},
            ):
                assert (await client.post(AGENT + "/command", json=payload)).status_code == 422
            assert (await client.post(AGENT + "/command", json=commands[0],
                                      headers={"Origin": "https://foreign.test"})).status_code == 403
            expected = []
            for command in commands:
                reply = await client.post(AGENT + "/command", json=command)
                assert reply.status_code == 202
                expected.append({**command, "id": reply.json()["id"]})
            result_path = f"/commands/{expected[0]['id']}/result"
            result = {"ok": True, "message": "完成"}
            assert (await client.post(AGENT + result_path, json=result, headers=AUTH)).status_code == 404
            assert (await client.post("/api/agents/other/heartbeat", json=SNAPSHOT, headers=AUTH)).json() == {"commands": []}
            replies = await asyncio.gather(
                client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH),
                client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH),
            )
            assert [command for reply in replies for command in reply.json()["commands"]] == expected
            assert (await client.post(AGENT + result_path, json=result)).status_code == 401
            assert (await client.post("/api/agents/other" + result_path, json=result, headers=AUTH)).status_code == 404
            for _ in range(2):
                assert (await client.post(AGENT + result_path, json=result, headers=AUTH)).status_code == 204
            assert (await client.post(AGENT + result_path, json={"ok": False}, headers=AUTH)).status_code == 409
            assert (await client.post(AGENT + "/command", json={"action": "lock"})).status_code == 202
            now += 16
            assert (await client.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).json() == {"commands": []}
            statuses = (await client.get("/api/agents")).json()["devices"][0]["commands"]
            assert [row["status"] for row in statuses] == ["succeeded", *["delivered"] * 7, "expired"]
            now += 60
            statuses = (await client.get("/api/agents")).json()["devices"][0]["commands"]
            assert all(row["status"] == "expired" for row in statuses[1:])

    asyncio.run(verify())


def prepare_life(tmp_path):
    store = Scratchpad(tmp_path)
    store.initialize()
    return LifeStore(store.connect, ZoneInfo("America/Toronto"))


def ack(change, external_id):
    return {"local_id": change["local_id"], "external_id": external_id, "updated_at": change["updated_at"]}


def test_reminders_old_database_and_outbound_retries(tmp_path, monkeypatch):
    life = prepare_life(tmp_path)
    day = life.day()
    previous = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    with life.connect() as db:
        db.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, day TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
        db.executemany("INSERT INTO todos(title,day) VALUES (?,?)", [("Today", day), ("Older", previous)])
    life.initialize()
    life.initialize()

    async def verify():
        async with client_for(tmp_path) as client:
            assert (await client.post(SYNC, json={})).status_code == 401
            created, = (await client.post(SYNC, json={}, headers=AUTH)).json()["changes"]
            assert created["action"] == "create" and created["title"] == "Today"
            assert (await client.get("/api/todos")).json()["items"] == [{"id": 1, "title": "Today", "done": False}]
            assert (await client.put("/api/todos/1/check", json={"done": True})).status_code == 204
            unacked = (await client.post(SYNC, json={}, headers=AUTH)).json()["changes"]
            assert len(unacked) == 1 and unacked[0]["local_id"] == created["local_id"] and unacked[0]["done"]
            monkeypatch.setattr(LifeStore, "day", lambda self, moment=None: "2099-01-02")
            assert (await client.post(SYNC, json={}, headers=AUTH)).json()["changes"] == unacked
            reply = await client.post(SYNC, json={"acknowledgements": [ack(created, "apple-1")]}, headers=AUTH)
            update, = reply.json()["changes"]
            assert update["action"] == "update" and update["done"] and update["external_id"] == "apple-1"
            reply = await client.post(SYNC, json={"acknowledgements": [ack(update, "apple-1")]}, headers=AUTH)
            assert reply.json() == {"changes": [], "has_more": False}

    asyncio.run(verify())


@pytest.mark.parametrize("local_deletes", [True, False])
def test_reminders_newest_wins_and_stale_data_cannot_resurrect_tombstone(tmp_path, local_deletes):
    prepare_life(tmp_path).initialize()
    item = {"external_id": "apple-record", "title": "Mac reminder",
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}

    async def verify():
        async with client_for(tmp_path) as client:
            async def sync(items=(), acknowledgements=()):
                reply = await client.post(SYNC, json={"items": items, "acknowledgements": acknowledgements}, headers=AUTH)
                assert reply.status_code == 200, reply.text
                return reply.json()["changes"]

            assert await sync([item, item]) == []
            row, = (await client.get("/api/todos")).json()["items"]
            assert row["external_source"] == "apple_reminders"
            target = f"/api/todos/{row['id']}"
            assert (await client.patch(target, json={"title": "Local rename"})).status_code == 204
            local, = await sync([item])
            assert local["action"] == "update" and local["title"] == "Local rename"
            newer = {**item, "title": "New from Apple",
                     "updated_at": (datetime.fromisoformat(local["updated_at"]) + timedelta(microseconds=1)).isoformat()}
            assert await sync([newer]) == []
            assert (await client.get("/api/todos")).json()["items"][0]["title"] == "New from Apple"
            if local_deletes:
                assert (await client.delete(target)).status_code == 204
                tombstone, = await sync([newer])
                assert tombstone["action"] == "delete" and tombstone["deleted"]
                assert await sync([item], [ack(tombstone, item["external_id"])]) == []
            else:
                tombstone = {"external_id": item["external_id"], "deleted": True,
                             "updated_at": datetime.now(timezone.utc).isoformat()}
                assert await sync([tombstone, newer]) == []
            assert (await client.get("/api/todos")).json()["items"] == []

    asyncio.run(verify())
