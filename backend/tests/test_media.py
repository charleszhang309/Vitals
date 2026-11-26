"""Windows 媒体会话、歌词缓存、Core Audio 和 Tailnet 路由。"""

import asyncio
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from functools import wraps
from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException

from backend.config import Settings
from backend.main import create_app
from backend.lyrics import LyricsCache, Track, candidate_score, parse_lrc, resolve_source, search_qq, fetch_qq, search_title
from backend.media import MediaAction, MediaBridge, VolumeChange
from backend.media_desktop import WindowsAudio, WindowsMedia
from backend.scratchpad import Scratchpad


def async_test(coroutine):
    @wraps(coroutine)
    def run(*args, **kwargs):
        return asyncio.run(coroutine(*args, **kwargs))
    return run


def session(app_id, title, status):
    info = SimpleNamespace(playback_status=status, playback_rate=1, controls=SimpleNamespace(
        is_play_enabled=True, is_pause_enabled=True, is_next_enabled=True, is_playback_position_enabled=True))
    timeline = SimpleNamespace(start_time=timedelta(seconds=10), end_time=timedelta(seconds=210),
                               position=timedelta(seconds=30), min_seek_time=timedelta(seconds=15),
                               max_seek_time=timedelta(seconds=180), last_updated_time=datetime.now(timezone.utc))
    return SimpleNamespace(source_app_user_model_id=app_id, get_playback_info=lambda: info,
                           get_timeline_properties=lambda: timeline, try_skip_next_async=AsyncMock(return_value=True),
                           try_change_playback_position_async=AsyncMock(return_value=True),
                           try_get_media_properties_async=AsyncMock(return_value=SimpleNamespace(
                               title=title, artist="Sample artist", album_title="Sample album", thumbnail=None)))


@async_test
async def test_smtc_session_choice_is_stable_and_uses_native_seek_positions():
    chrome, second_tab = session("Chrome.exe", "Fixture one", 4), session("Chrome.exe", "Fixture two", 5)
    apple = session("AppleInc.AppleMusicWin_8wekyb3d8bbwe!App", "Fixture three", 5)
    media = WindowsMedia()
    media.manager = SimpleNamespace(get_sessions=lambda: [chrome, second_tab, apple], get_current_session=lambda: apple)
    snapshot = await media.state()
    first, second, current = snapshot["sessions"]
    assert [item["player"] for item in snapshot["sessions"]] == ["Chrome", "Chrome", "Apple Music"]
    assert snapshot["current_session_id"] == current["session_id"]
    assert first["status"] == "playing" and second["status"] == "paused"
    assert second["position_seconds"] == 20 and second["duration_seconds"] == 200
    assert first["session_id"] != second["session_id"]

    await media.act(MediaAction(action="next", session_id=second["session_id"]))
    second_tab.try_skip_next_async.assert_awaited_once()
    chrome.try_skip_next_async.assert_not_awaited()
    assert (await media.state())["sessions"][1]["session_id"] == second["session_id"]
    await media.act(MediaAction(action="seek", session_id=second["session_id"], track_id=second["track_id"], position_seconds=35))
    second_tab.try_change_playback_position_async.assert_awaited_once_with(450000000)
    media.manager.get_sessions = lambda: [chrome, apple]
    with pytest.raises(HTTPException) as gone:
        await media.act(MediaAction(action="next", session_id=second["session_id"]))
    assert gone.value.status_code == 404


@async_test
async def test_smtc_keeps_late_artwork_for_the_same_track():
    native = session("Chrome.exe", "Fixture", 4)
    media = WindowsMedia()
    media.manager = SimpleNamespace(get_sessions=lambda: [native], get_current_session=lambda: native)
    original = (await media.state())["sessions"][0]
    native.try_get_media_properties_async.return_value.thumbnail = object()
    media.refresh_after = 0
    with patch("backend.media_desktop.artwork_bytes", new=AsyncMock(return_value=b"webp")) as read:
        updated = (await media.state())["sessions"][0]
        assert updated["track_id"] == original["track_id"]
        assert updated["has_artwork"]
        assert await media.artwork(updated["session_id"], updated["artwork_version"]) == b"webp"
        read.assert_awaited_once()


def test_audio_uses_per_call_com_initialization_and_preserves_unspecified_settings():
    endpoint = SimpleNamespace(SetMasterVolumeLevelScalar=Mock(), SetMute=Mock(),
                               GetMasterVolumeLevelScalar=lambda: .73, GetMute=lambda: 1)
    audio = WindowsAudio.__new__(WindowsAudio)
    audio.lock = Lock()
    audio.audio_errors = audio.endpoint_errors = (OSError,)
    audio.initialize, audio.uninitialize = Mock(), Mock()
    speakers = SimpleNamespace(id="native-speakers", FriendlyName="Speakers", EndpointVolume=endpoint)
    headphones_endpoint = SimpleNamespace(GetMasterVolumeLevelScalar=lambda: .26, GetMute=lambda: 0)
    headphones = SimpleNamespace(id="native-headphones", FriendlyName="Headphones", EndpointVolume=headphones_endpoint)
    audio.outputs = Mock(return_value=[speakers, headphones])
    audio.speakers = Mock(return_value=speakers)
    audio.switch = Mock(side_effect=lambda _: setattr(audio.speakers, "return_value", headphones))
    speakers_id, headphones_id = audio.device_key(speakers), audio.device_key(headphones)
    assert audio.volume(VolumeChange(percent=73)) == {
        "available": True, "percent": 73, "muted": True, "device_id": speakers_id,
        "devices": [{"id": speakers_id, "name": "Speakers"}, {"id": headphones_id, "name": "Headphones"}],
    }
    endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(.73, None)
    endpoint.SetMute.assert_not_called()
    audio.volume(VolumeChange(muted=False))
    endpoint.SetMute.assert_called_once_with(0, None)
    switched = audio.volume(VolumeChange(device_id=headphones_id))
    assert (switched["device_id"], switched["percent"], switched["muted"]) == (headphones_id, 26, False)
    with pytest.raises(HTTPException) as disconnected:
        audio.volume(VolumeChange(device_id="e" * 64))
    assert disconnected.value.status_code == 409
    audio.switch.assert_called_once_with("native-headphones")
    audio.speakers.side_effect = OSError("endpoint disappeared")
    snapshot = audio.volume()
    assert not snapshot["available"] and len(snapshot["devices"]) == 2
    assert audio.initialize.call_count == audio.uninitialize.call_count == 5


def test_lrc_offsets_multiple_timestamps_and_empty_lines():
    assert parse_lrc("[ti:测试]\n[01:02.300]第二行\n[00:00.100][00:12.45]第一行\n[00:23.3]\n[offset:-200]") == [
        {"time": 0.0, "text": "第一行"}, {"time": 12.25, "text": "第一行"},
        {"time": 23.1, "text": ""}, {"time": 62.1, "text": "第二行"},
    ]


@async_test
async def test_lrclib_matches_precisely_and_caches_hits_and_misses(tmp_path):
    store = Scratchpad(tmp_path)
    store.initialize()
    cache = LyricsCache(store)
    track = {"title": "Test Song", "artist": "Test Artist", "album": "Test Album", "duration_seconds": 190.2}
    paths = []

    def provider(request):
        for host, contents in {
            "c.y.qq.com": {"data": {"song": {"list": []}}},
            "mobileservice.kugou.com": {"data": {"info": []}},
            "music.163.com": {"code": 200, "result": {"songs": []}},
        }.items():
            if request.url.host == host:
                return httpx.Response(200, json=contents)
        paths.append(request.url.path)
        assert request.headers["user-agent"].startswith("Vitals/")
        if request.url.params["track_name"] == "Missing":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[
            {"trackName": "Test Song", "artistName": "Wrong Artist", "duration": 190, "plainLyrics": "incorrect"},
            {"trackName": "Test Song", "artistName": "Test Artist", "duration": 220, "plainLyrics": "incorrect"},
            {"trackName": "Test Song", "artistName": "Test Artist", "duration": 190, "syncedLyrics": "[00:12.34]正确"},
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        first, second = await asyncio.gather(cache.get(client, track), cache.get(client, {**track, "title": "Ｔｅｓｔ Song"}))
        assert paths == ["/api/search"]
        assert first == second and first["lines"] == [{"time": 12.34, "text": "正确"}]
        assert first["source"] == "lrclib"
        assert await LyricsCache(store).get(client, track, refresh=True) == first
        missing = {**track, "title": "Missing"}
        assert (await cache.get(client, missing))["status"] == "missing"
        assert (await LyricsCache(store).get(client, missing))["status"] == "missing"
        assert paths == ["/api/search"] * 3


def test_lyrics_search_terms_and_candidate_versions():
    assert search_title("Ｓｏ Far Away (feat. Jamie Scott & Romy Dya) [2017 Remastered]") == "So Far Away"
    assert search_title("So Far Away — 2017 Remaster") == "So Far Away"
    assert search_title("So Far Away - feat. Jamie Scott") == "So Far Away"
    assert search_title("Armageddon （Live）") == "Armageddon (Live)"
    studio = Track.from_state({"title": "Armageddon [Remastered]", "artist": "aespa", "album": "Armageddon - Single",
                               "duration_seconds": 196})
    assert candidate_score(studio, Track("Armageddon (世界末日)", "aespa (에스파)", "Armageddon", 197)) >= 95
    assert candidate_score(studio, Track("Armageddon (Live)", "aespa", "Armageddon", 196)) == 0
    assert candidate_score(studio, Track("Armageddon", "aespa", "Live at Wembley", 196)) == 0
    assert candidate_score(Track("Armageddon - Live", "aespa", "", 196), studio) == 0
    assert candidate_score(studio, Track("Armageddon", "Wrong Artist", "Armageddon", 196)) < 80
    assert candidate_score(studio, Track("Armageddon", "aespa", "Other Album", 208)) < 80
    assert candidate_score(studio, Track("Armageddon", "aespa", "Armageddon", 203)) in range(80, 95)
    assert candidate_score(Track("Live Forever", "Oasis"), Track("Live Forever", "Oasis")) == 100


@async_test
async def test_lyrics_title_only_fallback_checks_cross_script_identity():
    track = Track("玉蝴蝶", "Nicholas Tse", "玉蝴蝶", 290)
    studio = Track("玉蝴蝶", "谢霆锋", "玉蝴蝶", 288)
    assert 80 <= candidate_score(track, studio) < 95
    assert candidate_score(track, Track("玉蝴蝶", "謝霆鋒", "容祖兒演唱會 世界巡迴", 290)) == 0
    assert candidate_score(track, Track("玉蝴蝶", "謝霆鋒", "玉蝴蝶", 300)) == 0
    assert candidate_score(track, Track("玉蝴蝶", "謝霆鋒", "Unrelated album", 290)) == 0
    assert candidate_score(track, Track("玉蝴蝶", "Unrelated Artist", "玉蝴蝶", 290)) == 0
    queries = []

    def provider(request):
        assert request.headers["referer"] == "https://y.qq.com/"
        if request.url.path.endswith("search_for_qq_cp"):
            query = request.url.params["w"]
            queries.append(query)
            items = [] if "nicholas tse" in query else [
                {"songname": "玉蝴蝶 (Live)", "singer": [{"name": "谢霆锋"}], "interval": 290, "songmid": "live"},
                {"songname": studio.title, "singer": [{"name": studio.artist}], "albumname": studio.album,
                 "interval": studio.duration, "songmid": "studio"},
            ]
            return httpx.Response(200, json={"data": {"song": {"list": items}}})
        assert request.url.params["songmid"] == "studio"
        return httpx.Response(200, json={"lyric": b64encode("[00:06.20]测试歌词".encode()).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result, failed = await resolve_source(client, track, search_qq, fetch_qq)
    assert queries == ["玉蝴蝶 nicholas tse", "玉蝴蝶"]
    assert not failed and result[1]["lines"] == [{"time": 6.2, "text": "测试歌词"}]


@async_test
async def test_lyrics_sources_start_in_parallel_and_apply_confidence_then_priority(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.lyrics.SOURCE_TIMEOUT", .6)
    store = Scratchpad(tmp_path)
    store.initialize()
    cache = LyricsCache(store)
    started = set()
    ready = asyncio.Event()
    served = []
    qq_duration = 204
    slow = asyncio.Event()
    lrc = b64encode(b"[00:04.20]Synthetic line").decode()
    lookup = {"title": "Armageddon (feat. Guest)", "artist": "aespa", "album": "Armageddon", "duration_seconds": 196}

    async def provider(request):
        host, path = request.url.host, request.url.path
        if path in {"/soso/fcgi-bin/search_for_qq_cp", "/api/v3/search/song", "/api/cloudsearch/pc", "/api/search"}:
            assert "feat" not in str(request.url).casefold()
            started.add(host)
            if len(started) == 4:
                ready.set()
            await ready.wait()
        if host == "c.y.qq.com":
            if path.startswith("/soso"):
                return httpx.Response(200, json={"data": {"song": {"list": [
                    {"songname": "Armageddon (Live)", "singer": [{"name": "aespa"}], "interval": 196,
                     "albumname": "Armageddon", "songmid": "rejected"},
                    {"songname": "Armageddon", "singer": [{"name": "aespa"}], "interval": qq_duration,
                     "albumname": "Armageddon", "songmid": "acceptable"},
                ]}}})
            served.append(request.url.params["songmid"])
            assert request.url.params["songmid"] == "acceptable"
            return httpx.Response(200, json={"lyric": lrc})
        if host == "mobileservice.kugou.com":
            return httpx.Response(200, json={"data": {"info": [
                {"hash": "recording", "songname": "Armageddon", "singername": "aespa", "duration": 196},
            ]}})
        if host == "lyrics.kugou.com":
            if path == "/search":
                assert request.url.params["hash"] == "recording"
                return httpx.Response(200, json={"candidates": [
                    {"id": "42", "accesskey": "key", "song": "Armageddon", "singer": "aespa", "duration": 196000},
                ]})
            assert request.url.params["id"] == "42"
            if request.url.params["fmt"] == "krc":
                return httpx.Response(200, json={"content": "invalid KRC; fall back to LRC"})
            assert request.url.params["fmt"] == "lrc"
            served.append("kugou")
            return httpx.Response(200, json={"content": lrc})
        if host == "music.163.com":
            await slow.wait()
        return httpx.Response(200, json=[
            {"trackName": "Armageddon", "artistName": "aespa", "duration": 196, "syncedLyrics": "[00:00.00]Fallback"},
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await cache.get(client, lookup)
        assert started == {"c.y.qq.com", "mobileservice.kugou.com", "music.163.com", "lrclib.net"}
        assert sorted(served) == ["acceptable", "kugou"]
        assert result["source"] == "kugou" and result["lines"] == [{"time": 4.2, "text": "Synthetic line"}]
        qq_duration = 196
        started.clear()
        ready.clear()
        assert (await cache.get(client, {**lookup, "duration_seconds": 197}))["source"] == "qqmusic"


@async_test
async def test_lyrics_negative_cache_retry_and_success_are_shared(tmp_path):
    store = Scratchpad(tmp_path)
    store.initialize()
    track = {"title": "Song", "artist": "Artist", "duration_seconds": 150}
    missing = {"status": "missing", "lines": [], "plain": None}
    found = {"status": "synced", "lines": [{"time": 0, "text": "Line"}], "plain": None, "source": "netease"}
    with patch("backend.lyrics.find_lyrics", new=AsyncMock(side_effect=[missing, found])) as find:
        cache = LyricsCache(store)
        assert await cache.get(None, track) == missing
        assert await LyricsCache(store).get(None, track) == missing
        assert find.await_count == 1
        assert await cache.get(None, track, refresh=True) == found
        assert await LyricsCache(store).get(None, track) == found
        assert await cache.get(None, track, refresh=True) == found
        assert find.await_count == 2


@async_test
async def test_lyrics_retry_endpoint_keeps_identity_and_origin_guard(tmp_path):
    session_id, track_id = "a" * 32, "b" * 64
    track = {"session_id": session_id, "track_id": track_id}
    bridge = MediaBridge()
    bridge.state = AsyncMock(return_value={"current_session_id": session_id, "sessions": [track]})
    cache = Mock(get=AsyncMock(return_value={"status": "missing", "lines": [], "plain": None}))
    with (patch("backend.main.host_identity", return_value={"platform": "windows", "name": "Fixture"}),
          patch("backend.main.MediaBridge", return_value=bridge), patch("backend.main.LyricsCache", return_value=cache)):
        app = create_app(Settings(), tmp_path)
    external = object()
    app.state.external_client = external
    path = f"/api/media/lyrics?track_id={track_id}&refresh=1"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://demo.example-tail.ts.net") as client:
        assert (await client.get(path, headers={"Origin": "https://external.invalid"})).status_code == 403
        reply = await client.get(path, headers={"Origin": "https://demo.example-tail.ts.net"})
        assert reply.status_code == 200 and reply.json()["status"] == "missing"
        assert (await client.get(path.replace(track_id, "d" * 64))).json()["status"] == "missing"
    cache.get.assert_awaited_once_with(external, track, refresh=True)


@async_test
async def test_public_media_endpoints_cover_cache_and_volume_contract(tmp_path):
    identifier, version = "a" * 32, "b" * 64
    output_id = "c" * 64
    track = {"session_id": identifier, "track_id": version, "artwork_version": version, "has_artwork": True}
    bridge = MediaBridge()
    bridge.call = AsyncMock(side_effect=[
        httpx.Response(200, json={"available": True, "active": True, "current_session_id": identifier, "sessions": [track]}),
        httpx.Response(200, json={"available": True, "active": True, "current_session_id": identifier, "sessions": [track]}),
        httpx.Response(200, content=b"sample-webp"),
        httpx.Response(200, json={"available": True, "active": True, "current_session_id": identifier, "sessions": [track]}),
        httpx.Response(200, json={"available": True, "percent": 31, "muted": False}),
        httpx.Response(200, json={"available": True, "percent": 40, "muted": True, "device_id": output_id}),
        httpx.Response(200, json={"accepted": True}),
    ])
    with (patch("backend.main.host_identity", return_value={"platform": "windows", "name": "Fixture"}),
          patch("backend.main.MediaBridge", return_value=bridge)):
        app = create_app(Settings(), tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
        assert (await client.get("/api/media/state")).json()["active"]
        art_url = f"/api/media/artwork?session_id={identifier}&version={version}"
        image = await client.get(art_url)
        assert image.headers["content-type"] == "image/webp" and "immutable" in image.headers["cache-control"]
        unchanged = await client.get(art_url, headers={"If-None-Match": image.headers["etag"]})
        assert unchanged.status_code == 304
        assert (await client.get("/api/media/volume")).json()["percent"] == 31
        change = {"percent": 40, "muted": True, "device_id": output_id}
        switched = (await client.post("/api/media/volume", json=change)).json()
        assert switched["muted"] and switched["device_id"] == output_id
        assert bridge.call.await_args_list[-1].kwargs["json"] == change
        assert (await client.post("/api/media/action", json={"action": "toggle", "session_id": identifier})).json()["accepted"]
        assert (await client.post("/api/media/action", json={"action": "exec", "session_id": identifier})).status_code == 422
        assert (await client.post("/api/media/volume", json={"percent": 101})).status_code == 422
        assert (await client.post("/api/media/volume", json={"device_id": "device id"})).status_code == 422
        assert (await client.post("/api/media/volume", json={"percent": 40}, headers={"Origin": "https://other.test"})).status_code == 403
        assert bridge.call.await_count == 7


@async_test
async def test_media_idle_when_interactive_bridge_is_unavailable(tmp_path):
    with (patch("backend.main.host_identity", return_value={"platform": "windows", "name": "Fixture"}),
          patch("backend.main.MediaBridge", return_value=MediaBridge())):
        app = create_app(Settings(), tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://localhost") as client:
        assert (await client.get("/api/media/state")).json() == {
            "available": True, "active": False, "current_session_id": None, "sessions": [], "reason": "desktop_unavailable",
        }
        assert not (await client.get("/api/media/volume")).json()["available"]
        assert (await client.get("/api/media/lyrics")).json()["status"] == "missing"
