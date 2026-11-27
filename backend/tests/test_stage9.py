import asyncio
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.agents import AgentRegistry, ButtonCommand, Heartbeat
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


def agent_app(tmp_path, token="fixture-agent-token"):
    settings = Settings(agents={"token": token}, health={"timezone": "America/Toronto"})
    return create_app(settings, tmp_path, static_dir=tmp_path / "no-static")


def client_for_app(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def client_for(tmp_path, token="fixture-agent-token"):
    return client_for_app(agent_app(tmp_path, token))


def test_device_aliases_and_last_snapshot_survive_restart(tmp_path):
    async def verify():
        app = agent_app(tmp_path)
        async with app.router.lifespan_context(app), client_for_app(app) as api:
            route = "/api/agents/aliases"
            assert (await api.get(route)).json() == {"aliases": {}}
            for device_id, alias in (("hub", " 主机 "), ("macbook", " 笔记本 ")):
                response = await api.patch(route, json={"device_id": device_id, "alias": alias})
                assert response.json()["aliases"][device_id] == alias.strip()
            assert (await api.patch(route, json={"device_id": "hub", "alias": " "})).status_code == 422
            assert (await api.patch(route, json={"device_id": "hub", "alias": "坏的请求"},
                                    headers={"Origin": "https://outside.example"})).status_code == 403
            assert (await api.post("/api/agents/hub/heartbeat", json=SNAPSHOT, headers=AUTH)).status_code == 422
            (await api.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).raise_for_status()
            before = (await api.get("/api/agents")).json()["devices"][0]
            assert before["name"] == "笔记本" and before["snapshot"]["name"] == "MacBook"
            assert before["online"]

        restarted = agent_app(tmp_path)
        async with restarted.router.lifespan_context(restarted), client_for_app(restarted) as api:
            assert (await api.get(route)).json() == {"aliases": {"hub": "主机", "macbook": "笔记本"}}
            restored = (await api.get("/api/agents")).json()["devices"][0]
            assert not restored["online"] and restored["last_seen"] == before["last_seen"]
            assert restored["snapshot"] == before["snapshot"] and restored["commands"] == []
            assert (await api.patch(route, json={"device_id": "macbook", "alias": None})).status_code == 200
            assert (await api.get("/api/agents")).json()["devices"][0]["name"] == "MacBook"

    asyncio.run(verify())


def test_delete_only_offline_devices_and_clear_profile(tmp_path, monkeypatch):
    now = 100
    monkeypatch.setattr("backend.agents.monotonic", lambda: now)

    async def verify():
        nonlocal now
        app = agent_app(tmp_path)
        async with app.router.lifespan_context(app), client_for_app(app) as api:
            (await api.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).raise_for_status()
            (await api.patch("/api/agents/aliases", json={"device_id": "macbook", "alias": "笔记本"})).raise_for_status()
            assert (await api.post(AGENT + "/command", json={"action": "media_next"})).status_code == 202
            assert (await api.delete("/api/agents/hub")).status_code == 409
            assert (await api.delete(AGENT)).status_code == 409
            now += 16
            assert (await api.delete(AGENT, headers={"Origin": "https://outside.example"})).status_code == 403
            assert (await api.delete(AGENT)).status_code == 204
            assert (await api.delete(AGENT)).status_code == 404
            assert (await api.get(AGENT + "/artwork")).status_code == 404
            assert (await api.get("/api/agents/aliases")).json() == {"aliases": {}}

        restarted = agent_app(tmp_path)
        async with restarted.router.lifespan_context(restarted), client_for_app(restarted) as api:
            assert (await api.get("/api/agents")).json() == {"devices": []}
            assert (await api.post(AGENT + "/heartbeat", json=SNAPSHOT, headers=AUTH)).json() == {"commands": []}
            device = (await api.get("/api/agents")).json()["devices"][0]
            assert device["name"] == "MacBook" and device["online"]

    asyncio.run(verify())


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


def test_microphone_commands_require_input_resources_and_accept_legacy_heartbeats(tmp_path):
    commands = {"input_volume_set": {"percent": 0}, "input_volume_mute": {"muted": False},
                "input_audio_device_set": {"device_id": "fixture:mic"}}

    async def verify():
        async with client_for(tmp_path) as client:
            async def heartbeat(snapshot):
                response = await client.post(AGENT + "/heartbeat", json=snapshot, headers=AUTH)
                response.raise_for_status()
                return response.json()["commands"]

            await heartbeat(SNAPSHOT)
            for action, payload in commands.items():
                reply = await client.post(AGENT + "/command", json={"action": action, "payload": payload})
                assert reply.status_code == 409
            legacy = await client.post(AGENT + "/command", json={"action": "volume_set", "payload": {"percent": 51}})
            assert legacy.status_code == 202
            assert [item["action"] for item in await heartbeat(SNAPSHOT)] == ["volume_set"]

            volume = {**SNAPSHOT["volume"], "device_id": "fixture:speakers",
                      "devices": [{"id": "fixture:speakers", "name": "Speakers"}],
                      "input_percent": 0, "input_muted": False, "input_device_id": "fixture:mic",
                      "input_devices": [{"id": "fixture:mic", "name": "Microphone"}]}
            snapshot = {**SNAPSHOT, "volume": volume}
            assert await heartbeat(snapshot) == []
            public = (await client.get("/api/agents")).json()["devices"][0]["snapshot"]["volume"]
            assert public == volume
            wrong_direction = await client.post(AGENT + "/command", json={
                "action": "input_audio_device_set", "payload": {"device_id": "fixture:speakers"}})
            assert wrong_direction.status_code == 409
            invalid = await client.post(AGENT + "/command", json={"action": "input_volume_mute", "payload": {"muted": 0}})
            assert invalid.status_code == 422
            for action, payload in commands.items():
                reply = await client.post(AGENT + "/command", json={"action": action, "payload": payload})
                assert reply.status_code == 202
            assert {cmd["action"]: cmd["payload"] for cmd in await heartbeat(snapshot)} == commands

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


@asynccontextmanager
async def connect_command_stream(app, path):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    incoming.put_nowait({"type": "http.request"})
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1234),
        "headers": [(b"host", b"127.0.0.1"), (b"authorization", AUTH["Authorization"].encode())],
    }
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))

    async def read():
        return await asyncio.wait_for(outgoing.get(), 1)

    try:
        start = await read()
        assert start["status"] == 200
        assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")
        assert (await read())["body"] == b": connected\n\n"
        yield read
    finally:
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 1)


def test_command_stream_wakes_the_target_without_waiting_for_a_heartbeat(tmp_path):
    path = AGENT + "/commands/stream"
    other = "/api/agents/other"

    async def verify():
        app = agent_app(tmp_path)
        async with client_for_app(app) as client:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=AUTH)).status_code == 404
            for device in (AGENT, other):
                assert (await client.post(device + "/heartbeat", headers=AUTH, json=SNAPSHOT)).status_code == 200
            async with (connect_command_stream(app, other + "/commands/stream") as read_other,
                        connect_command_stream(app, path) as read):
                response = await client.post(AGENT + "/command", json={"action": "volume_set", "payload": {"percent": 35}})
                assert response.status_code == 202
                identifier = response.json()["id"]
                body = (await read())["body"]
                assert body.startswith(b"event: command\ndata: ") and body.endswith(b"\n\n")
                assert json.loads(body.split(b"data: ", 1)[1]) == {"id": identifier, "action": "volume_set", "payload": {"percent": 35}}
                response = await client.post(other + "/command", json={"action": "media_next"})
                body = (await read_other())["body"]
                assert json.loads(body.split(b"data: ", 1)[1])["id"] == response.json()["id"]
                heartbeat = await client.post(AGENT + "/heartbeat", headers=AUTH, json=SNAPSHOT)
                assert heartbeat.json()["commands"] == []
                result = await client.post(f"{AGENT}/commands/{identifier}/result", headers=AUTH, json={"ok": True})
                assert result.status_code == 204
                devices = (await client.get("/api/agents")).json()["devices"]
                assert devices[0]["commands"][0]["completed_at"]

            # A new command returns in the next heartbeat after the stream closes.
            response = await client.post(AGENT + "/command", json={"action": "media_play_pause"})
            heartbeat = await client.post(AGENT + "/heartbeat", headers=AUTH, json=SNAPSHOT)
            assert [command["id"] for command in heartbeat.json()["commands"]] == [response.json()["id"]]

    asyncio.run(verify())


def test_command_stream_keepalive_and_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.agents.KEEPALIVE_SECONDS", 0.04)
    path = AGENT + "/commands/stream"

    async def verify():
        app = agent_app(tmp_path)
        async with client_for_app(app) as client:
            await client.post(AGENT + "/heartbeat", headers=AUTH, json=SNAPSHOT)
            async with connect_command_stream(app, path) as first:
                assert (await first())["body"] == b": keepalive\n\n"
                async with connect_command_stream(app, path) as second:
                    while (await first())["more_body"]:
                        pass
                    response = await client.post(AGENT + "/command", json={"action": "display_lock"})
                    body = (await second())["body"]
                    while body.startswith(b":"):
                        body = (await second())["body"]
                    assert json.loads(body.split(b"data: ", 1)[1])["id"] == response.json()["id"]

    asyncio.run(verify())


def test_stalled_command_stream_leaves_unclaimed_commands_for_heartbeat(monkeypatch):
    now = 100
    monkeypatch.setattr("backend.agents.monotonic", lambda: now)
    monkeypatch.setattr("backend.agents.KEEPALIVE_SECONDS", 0.04)
    registry = AgentRegistry()
    snapshot = Heartbeat.model_validate(SNAPSHOT)
    registry.heartbeat("macbook", snapshot)

    async def verify():
        nonlocal now
        async with aclosing(registry.stream("macbook")) as stream:
            assert await anext(stream) == b": connected\n\n"
            reply = registry.enqueue("macbook", ButtonCommand(action="lock"))
            assert registry.heartbeat("macbook", snapshot)["commands"] == []
            now += 1
            assert registry.heartbeat("macbook", snapshot)["commands"] == [
                {"id": reply["id"], "action": "lock", "payload": {}},
            ]
            assert await asyncio.wait_for(anext(stream), 1) == b": keepalive\n\n"

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


def test_remote_media_artwork_and_upgrade_commands(tmp_path, monkeypatch):
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image
    from backend.lyrics import LyricsCache

    artwork = BytesIO()
    Image.new("RGB", (64, 32), "blue").save(artwork, "JPEG")
    contents = artwork.getvalue()
    track_id = "a" * 64
    version = sha256(track_id.encode() + contents).hexdigest()
    snapshot = {**SNAPSHOT, "caffeinate": False,
                "volume": {**SNAPSHOT["volume"], "device_id": "BuiltInSpeakerDevice", "devices": [
                    {"id": "BuiltInSpeakerDevice", "name": "MacBook Speakers"},
                    {"id": "fixture:headphones", "name": "Headphones"},
                ]},
                "media": {**SNAPSHOT["media"], "album": "Sample album", "track_id": track_id,
                          "artwork_version": version, "position_seconds": 6, "duration_seconds": 200,
                          "updated_at": SNAPSHOT["timestamp"]}}

    async def mock_lyrics(self, client, track, *, refresh):
        return {"status": "synced", "album": track["album"]}

    monkeypatch.setattr(LyricsCache, "get", mock_lyrics)

    async def verify():
        app = create_app(Settings(agents={"token": "fixture-agent-token"}), tmp_path, static_dir=tmp_path / "no-static")
        app.state.external_client = object()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            async def heart(data):
                response = await client.post(AGENT + "/heartbeat", json=data, headers=AUTH)
                assert response.status_code == 200, response.text
                return response.json()

            async def read():
                return (await client.get("/api/agents")).json()["devices"][0]

            art_path = AGENT + "/artwork"
            query = {"version": version}
            upload_headers = {**AUTH, "Content-Type": "image/jpeg"}
            assert (await heart(snapshot))["artwork_needed"] is True
            assert (await read())["snapshot"]["media"]["artwork_version"] is None
            assert (await client.post(art_path, params=query, content=contents)).status_code == 401
            assert (await client.post(art_path, params=query, content=contents,
                                      headers={**AUTH, "Content-Type": "image/png"})).status_code == 415
            assert (await client.post(art_path, params=query, content=b"fake", headers=upload_headers)).status_code == 422
            assert (await client.post(art_path, params=query, content=contents, headers=upload_headers)).status_code == 204
            assert (await heart(snapshot))["artwork_needed"] is False
            assert (await read())["snapshot"]["media"]["artwork_version"] == version
            cover = await client.get(art_path, params=query)
            assert cover.content == contents and cover.headers["Content-Type"] == "image/jpeg"
            assert (await client.get(art_path, params=query, headers={"If-None-Match": cover.headers["etag"]})).status_code == 304
            current = await client.get(art_path)
            assert current.content == contents and current.headers["cache-control"] == "private, no-cache"
            assert (await client.get(art_path, params={"version": "b" * 64})).status_code == 404

            params = {"agent_id": "macbook", "track_id": track_id}
            assert (await client.get("/api/media/lyrics", params=params)).json() == {"status": "synced", "album": "Sample album"}
            assert (await client.get("/api/media/lyrics", params={**params, "track_id": "b" * 64})).json()["status"] == "missing"

            changes = {"display_lock": {}, "caffeinate_toggle": {}, "clipboard_push": {},
                       "sleep": {}, "audio_device_set": {"device_id": "fixture:headphones"}}
            for action, payload in changes.items():
                reply = await client.post(AGENT + "/command", json={"action": action, "payload": payload})
                assert reply.status_code == 202
            assert (await client.post(AGENT + "/command", json={"action": "audio_device_set", "payload": {}})).status_code == 422
            unavailable = {"action": "audio_device_set", "payload": {"device_id": "another-output"}}
            assert (await client.post(AGENT + "/command", json=unavailable)).status_code == 409
            assert {cmd["action"]: cmd["payload"] for cmd in (await heart(snapshot))["commands"]} == changes
            await heart({**snapshot, "media": None})
            assert (await client.get(art_path, params=query)).status_code == 404

    asyncio.run(verify())


def test_mac_agent_telemetry_and_metadata_lyrics_cache(tmp_path, monkeypatch):
    from backend.lyrics import Track

    settings = Settings(agents={"token": "fixture-agent-token"})
    Scratchpad(tmp_path).initialize()
    app = create_app(settings, tmp_path, static_dir=tmp_path / "no-static")
    app.state.external_client = object()
    interfaces = [{"name": "en0", "addresses": ["192.0.2.10"]},
                  {"name": "utun1", "addresses": ["2001:db8::7"], "tailscale": True}]
    snapshot = {
        **SNAPSHOT, "caffeinate": True,
        "battery": {**SNAPSHOT["battery"], "cycle_count": 45, "health_percent": 96, "temperature_c": 27.5},
        "cpu": {**SNAPSHOT["cpu"], "name": "Fixture CPU", "physical_cores": 8, "logical_cores": 8},
        "memory": {**SNAPSHOT["memory"], "available_bytes": 4},
        "disks": [{**SNAPSHOT["disks"][0], "name": "Macintosh HD"}],
        "network": {**SNAPSHOT["network"], "total_upload_bytes": 1000},
        "network_info": {"interfaces": interfaces},
        "volume": {"devices": [{"id": "fixture:usb", "name": "USB Audio"}], "device_id": "fixture:usb"},
        "media": {"title": "Fixture song", "artist": "Fixture artist", "album": "Fixture album",
                  "duration_seconds": 140, "position_seconds": 12, "status": "playing", "player": "Apple Music"},
    }
    result = {"status": "synced", "source": "amll", "amll_lines": [{"startTime": 1000}]}
    lookups = []

    async def find(client, identity, player):
        lookups.append((identity, player))
        return result

    monkeypatch.setattr("backend.lyrics.find_lyrics", find)

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            async def publish(data):
                assert (await client.post(AGENT + "/heartbeat", json=data, headers=AUTH)).status_code == 200
                return (await client.get("/api/agents")).json()["devices"][0]["snapshot"]

            observed = await publish(snapshot)
            assert observed["battery"]["cycle_count"] == 45 and observed["battery"]["health_percent"] == 96
            assert observed["cpu"]["physical_cores"] == 8 and observed["memory"]["available_bytes"] == 4
            assert observed["disks"][0]["name"] == "Macintosh HD" and observed["caffeinate"] is True
            assert observed["network"]["total_upload_bytes"] == 1000
            assert [item["addresses"] for item in observed["network"]["interfaces"]] == [["192.0.2.10"], ["2001:db8::7"]]
            assert observed["volume"]["percent"] is None and observed["volume"]["device_id"] == "fixture:usb"

            track_id = observed["media"]["track_id"]
            assert track_id and observed["media"]["position_seconds"] == 12
            updated = await publish({**snapshot, "network": {"interfaces": interfaces},
                                     "media": {**snapshot["media"], "position_seconds": 22}})
            assert updated["media"]["track_id"] == track_id
            query = {"agent_id": "macbook", "track_id": track_id}
            for _ in range(2):
                response = await client.get("/api/media/lyrics", params=query)
                assert response.json() == result
            assert lookups == [(Track("Fixture song", "Fixture artist", "Fixture album", 140), "apple music")]
            await publish({**snapshot, "media": {**snapshot["media"], "album": "Alternate album"}})
            assert (await client.get("/api/media/lyrics", params=query)).json()["status"] == "missing"

    asyncio.run(verify())
