import asyncio
from base64 import b64encode
import json
import zlib

import httpx
import pytest

from backend.lyric_timing import KRC_KEY, decode_krc
from backend.lyrics import Candidate, LyricsCache, Track, decode_qq_qrc, fetch_netease, find_lyrics, lyrics_result
from backend.scratchpad import Scratchpad


def test_lrc_stays_compatible_and_pairs_translations_by_time():
    result = lyrics_result("[00:05.00]First\n[00:05.55]Second\n[00:21.00]Last", source="qqmusic",
                           translated="[00:05.55]第二句\n[00:05.09]第一句\n[00:20.0]时间差过大")
    assert result["lines"][0] == {"time": 5.0, "text": "First"}
    first, second, last = result["amll_lines"]
    assert first["words"] == [{"startTime": 5000, "endTime": 5550, "word": "First"}]
    assert second["endTime"] == 10550
    assert [row["translatedLyric"] for row in (first, second, last)] == ["第一句", "第二句", ""]
    assert lyrics_result(plain="Only text")["status"] == "plain"


@pytest.mark.parametrize(("kind", "payload"), [
    ("yrc", "[5000,1500](5000,600,0)Hello (5600,900,0)world"),
    ("qrc", "[5000,1500]Hello (5000,600)world(5600,900)"),
    ("krc", "[5000,1500]<0,600,0>Hello <600,900,0>world"),
    (None, "[00:05.000]<00:05.000>Hello <00:05.600>world<00:06.500>"),
])
def test_word_level_timing_in_milliseconds(kind, payload):
    result = lyrics_result(timed=payload, kind=kind) if kind else lyrics_result(payload)
    line, = result["amll_lines"]
    assert (line["startTime"], line["endTime"]) == (5000, 6500)
    assert line["words"] == [
        {"startTime": 5000, "endTime": 5600, "word": "Hello "},
        {"startTime": 5600, "endTime": 6500, "word": "world"},
    ]
    assert result["lines"] == [{"time": 5.0, "text": "Hello world"}]


def test_krc_embedded_translations_match_original_order():
    translation = b64encode(json.dumps({"content": [
        {"type": 0, "lyricContent": [["ni"], ["hao"]]},
        {"type": 1, "lyricContent": [["你好"], ["再见"]]},
    ]}).encode()).decode()
    raw = f"[language:{translation}]\n[1000,600]<0,600,0>Hello\n[3000,600]<0,600,0>Bye"
    compressed = zlib.compress(raw.encode())
    encoded = b64encode(b"krc1" + bytes(byte ^ KRC_KEY[i % len(KRC_KEY)] for i, byte in enumerate(compressed)))
    recovered = decode_krc(encoded)
    lines = lyrics_result(timed=recovered, kind="krc")["amll_lines"]
    assert [row["translatedLyric"] for row in lines] == ["你好", "再见"]


def test_ttml_translation_and_background_vocals_are_separate():
    xml = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">
              <body><div><p begin="0:01.0" end="0:03.0">
                  <span begin="1s" end="1.5s">One </span><span begin="1.5s" end="3s">two</span>
                  <span ttm:role="x-translation" xml:lang="zh-Hans">一 二</span>
                  <span ttm:role="x-bg">Echo</span>
              </p></div></body></tt>"""
    line, echo = lyrics_result(timed=xml, kind="ttml")["amll_lines"]
    assert (line["startTime"], line["endTime"], line["translatedLyric"]) == (1000, 3000, "一 二")
    assert [w["startTime"] for w in line["words"]] == [1000, 1500]
    assert not line["isBG"] and echo["isBG"] and echo["words"][0]["word"] == "Echo"
    fallback = lyrics_result("[00:01]Line", timed='<!DOCTYPE tt [<!ENTITY text "bad">]><tt/>', kind="ttml")
    assert fallback["amll_lines"][0]["words"][0]["word"] == "Line"


TTML_WITH_TRANSLATION = """<tt xmlns:ttm="http://www.w3.org/ns/ttml#metadata" xmlns:itunes="urn:itunes">
  <head><metadata><iTunesMetadata><translations>
    <translation xml:lang="zh-Hans"><text for="L1">原文译文<span ttm:role="x-bg">和声译文</span></text></translation>
    <translation xml:lang="en"><text for="L1">Other translation</text></translation>
  </translations></iTunesMetadata></metadata></head>
  <body dur="2:00.000"><div><p begin="1.000" end="4.000" itunes:key="L1">
    <span begin="1.0" end="1.5">Hello </span>
    <span begin="1.5" end="3.0">world</span>
    <span ttm:role="x-bg"><span begin="3.0" end="3.5">Echo </span><span begin="3.5" end="4.0">again</span></span>
  </p></div></body>
</tt>"""


def test_ttml_resolves_header_translation_language_and_preserves_timed_backing_vocals():
    lead, echo = lyrics_result(timed=TTML_WITH_TRANSLATION, kind="ttml")["amll_lines"]
    assert (lead["translatedLyric"], echo["translatedLyric"]) == ("原文译文", "和声译文")
    assert [word["startTime"] for word in lead["words"]] == [1000, 1500]
    assert [word["startTime"] for word in echo["words"]] == [3000, 3500]
    assert echo["isBG"] and echo["endTime"] == 4000


@pytest.mark.parametrize(("player", "source_index"), [("qq 音乐", 0), ("酷狗音乐", 1), ("网易云音乐", 2)])
def test_player_specific_source_precedes_other_sources(monkeypatch, player, source_index):
    from backend import lyrics

    preferred = lyrics.SOURCES[source_index]
    called = []

    async def resolve(client, track, search, fetch):
        called.append((search, fetch))
        return (82, {"status": "plain", "plain": "Matched preferred source"}), False

    monkeypatch.setattr(lyrics, "resolve_source", resolve)
    result = asyncio.run(find_lyrics(None, Track("Fixture", "Guest"), player))
    assert called == [preferred]
    assert result["plain"] == "Matched preferred source"


@pytest.mark.parametrize("failure", [None, "weak", "duration", "timeout"])
def test_apple_music_ttml_lookup_and_default_fallback(failure):
    requests = []

    def provider(request):
        requests.append((request.url.host, request.url.path))
        if request.url.path == "/v1/lyrics/search":
            return httpx.Response(200, json={"data": {"items": [{"id": 7, "musicNames": ["Fixture"],
                "artistNames": ["Another" if failure == "weak" else "Guest"], "albumNames": ["Demo"]}]}})
        if request.url.path == "/v1/lyrics/get":
            assert request.url.params["id"] == "7"
            if failure == "timeout":
                raise httpx.ReadTimeout("offline")
            document = TTML_WITH_TRANSLATION.replace("2:00.000", "3:00.000") if failure == "duration" else TTML_WITH_TRANSLATION
            return httpx.Response(200, json={"data": {"lyrics": document}})
        if request.url.host == "lrclib.net":
            return httpx.Response(200, json=[{"trackName": "Fixture", "artistName": "Guest", "albumName": "Demo",
                                            "duration": 120, "plainLyrics": "Fallback"}])
        return httpx.Response(200, json={"data": {"song": {"list": []}, "info": []}, "result": {"songs": []}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            return await find_lyrics(client, Track("Fixture", "Guest", "Demo", 120), "apple music")

    result = asyncio.run(run())
    assert requests[0] == ("api.amll.dev", "/v1/lyrics/search")
    if failure is None:
        assert result["source"] == "amll" and result["amll_lines"][0]["translatedLyric"] == "原文译文"
        assert len(requests) == 2
    else:
        assert result["source"] == "lrclib" and result["status"] == "plain"
        if failure == "weak":
            assert ("api.amll.dev", "/v1/lyrics/get") not in requests


def test_player_scope_keeps_misses_and_matches_separate(monkeypatch, tmp_path):
    calls = []

    async def resolve(_client, track, preference):
        calls.append(preference)
        return {"status": "missing", "lines": [], "plain": None} if preference else lyrics_result(plain="Fixture")

    monkeypatch.setattr("backend.lyrics.find_lyrics", resolve)

    async def run():
        store = Scratchpad(tmp_path)
        store.initialize()
        cache = LyricsCache(store)
        apple = {"title": "Fixture", "artist": "Guest", "player": "Apple Music"}
        spotify = {**apple, "player": "Spotify"}
        assert (await cache.get(None, apple)) == {"status": "missing", "lines": [], "plain": None}
        assert (await cache.get(None, spotify))["status"] == "plain"
        assert (await cache.get(None, apple))["status"] == "missing"
        assert (await cache.get(None, {**spotify, "player": "Chrome"}))["status"] == "plain"

    asyncio.run(run())
    assert calls == ["apple music", ""]


def test_lrclib_lyricsfile_preserves_words_and_bad_document_falls_back():
    payload = """
version: '1.0'
lines:
  - text: 'Hello world'
    start_ms: 2000
    end_ms: 3200
    words:
      - {text: 'Hello ', start_ms: 2000, end_ms: 2300}
      - {text: world, start_ms: 2300, end_ms: 3200}
"""
    result = lyrics_result("[00:02]Hello world", timed=payload, kind="lyricsfile")
    assert [w["word"] for w in result["amll_lines"][0]["words"]] == ["Hello ", "world"]
    fallback = lyrics_result("[00:02]Hello world", timed="!!python/object:builtins.object {}", kind="lyricsfile")
    assert len(fallback["amll_lines"][0]["words"]) == 1


def test_qq_qrc_wrapper_preserves_newlines_and_plain_translation():
    sample = """<cmd><content type="file"><![CDATA[<QrcInfos>
    <Lyric_1 LyricContent="[1000,1600]Hello(1000,1600)
[3000,1600]Bye(3000,1600)" /></QrcInfos>]]></content>
    <contentts><![CDATA[[00:01]你好
[00:03]再见]]></contentts>"""
    timed, translated = decode_qq_qrc(sample)
    result = lyrics_result(timed=timed, translated=translated, kind="qrc")
    assert [line["translatedLyric"] for line in result["amll_lines"]] == ["你好", "再见"]


def test_netease_requests_word_and_translated_tracks_and_keeps_legacy_lines():
    def provider(request):
        assert request.url.params["yv"] == "1" and request.url.params["tv"] == "1"
        return httpx.Response(200, json={
            "lrc": {"lyric": "[00:01.010]Hello world"},
            "yrc": {"lyric": "[1000,1500](1000,500,0)Hello (1500,1000,0)world"},
            "tlyric": {"lyric": "[00:01.010]你好，世界"},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            return await fetch_netease(client, Candidate(Track("Fixture", "Guest"), 42))

    result = asyncio.run(run())
    assert result["lines"][0]["time"] == 1.01
    assert len(result["amll_lines"][0]["words"]) == 2
    assert result["amll_lines"][0]["translatedLyric"] == "你好，世界"
