"""多源歌词检索、版本校验和 SQLite 缓存。"""

import asyncio
from base64 import b64decode
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from hashlib import sha256
from html import unescape
import json
import math
from pathlib import Path
import re
from shutil import which
import subprocess
from time import time
import unicodedata
from xml.etree.ElementTree import ParseError
from zlib import error as ZlibError

import httpx
from yaml import YAMLError

from .lyric_timing import (MAX_LYRIC, align_translations, amll_timeline, decode_krc, krc_translations, parse_lrc,
                           parse_lrc_lines, parse_lyricsfile, parse_ttml, parse_word_timing, xml_root)


MIN_SCORE = 80
HIGH_SCORE = 95
SOURCE_TIMEOUT = 4.5
MISS_TTL = 8 * 60
ERROR_TTL = 60
EMPTY_LYRICS = {"status": "missing", "lines": [], "plain": None}
AMLL_API = "https://api.amll.dev/v1/lyrics"
SUFFIX = re.compile(r"\s*[\[({【]([^\])}】]+)[\])}】]\s*$")
NOISE = re.compile(r"^(?:(?:feat(?:uring)?|ft|with)\.?\s|(?:\d{4}\s+)?remaster(?:ed)?\b|explicit$|clean$)", re.I)
FEATURE = re.compile(r"\s+(?:[-–—]\s*)?(?:feat(?:uring)?|ft)\.?\s+.+$", re.I)
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]")
LIVE_CJK = re.compile(r"(?:现场(?:版)?|現場(?:版)?|演唱[会會]|现场实录|ライヴ|ライブ)$")
LIVE_ALBUM = re.compile(r"演唱[会會]|(?:现场实录|現場實錄)|\bconcert tour\b")
VERSION_TAGS = {
    "live": re.compile(r"^live(?:\b|$)|现场|現場|演唱[会會]|ライブ|ライヴ|\b(?:vma|award show)\b"),
    "remix": re.compile(r"\b(?:remix|rework|club mix|sped up|slowed(?: down)?)\b"),
    "acoustic": re.compile(r"\bacoustic\b|不插电"),
    "instrumental": re.compile(r"\b(?:karaoke|instrumental)\b|伴奏"),
    "demo": re.compile(r"\bdemo\b"),
}


def display_text(value):
    return unicodedata.normalize("NFKC", unescape(value[:800]))[:400].strip() if isinstance(value, str) else ""


def normalized(value):
    return " ".join(re.findall(r"[^\W_]+", display_text(value).casefold()))


def search_title(value):
    title = display_text(value)
    while True:
        suffix = SUFFIX.search(title)
        if suffix and NOISE.match(suffix[1]):
            title = title[:suffix.start()].strip()
            continue
        cleaned = FEATURE.sub("", title).strip()
        trailing = re.split(r"\s+[-–—]\s+", cleaned)
        if len(trailing) > 1 and NOISE.match(trailing[-1]):
            cleaned = cleaned[:cleaned.rfind(trailing[-1])].rstrip(" -–—")
        if cleaned == title:
            return title
        title = cleaned


def without_translation(value):
    suffix = SUFFIX.search(value)
    if suffix and CJK.search(suffix[1]) and not CJK.search(value[:suffix.start()]):
        return value[:suffix.start()].rstrip()
    return value


def version_tags(title, album):
    # 括号、分隔符和明确的标题后缀代表版本说明，保留《Live Forever》等歌名。
    title_parts = re.findall(r"[\[({【]([^\])}】]+)[\])}】]", title)
    title_parts.extend(re.split(r"\s+[-–—]\s+", title)[1:])
    hints = [normalized(part) for part in title_parts]
    for label in (normalized(title), normalized(album)):
        tail = re.search(r"\b(live(?: at| in| from| on) .+|live(?: version| recording| album)?|acoustic|instrumental|demo|remix)$", label)
        if tail:
            hints.append(tail[1])
        if LIVE_CJK.search(label):
            hints.append("live")
    if LIVE_ALBUM.search(normalized(album)):
        hints.append("live")
    return {tag for tag, pattern in VERSION_TAGS.items() if any(pattern.search(hint) for hint in hints)}


def seconds(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def artist_names(value):
    names = re.split(r"\s+(?:feat(?:uring)?|ft)\.?\s+|[,，、;&/]+|\s+[x×]\s+", display_text(value), flags=re.I)
    return tuple(dict.fromkeys(name for item in names if (name := normalized(without_translation(item.strip())))))


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str = ""
    duration: float | None = None

    @classmethod
    def from_state(cls, track):
        return cls(search_title(track.get("title")), display_text(track.get("artist")),
                   display_text(track.get("album")), seconds(track.get("duration_seconds")))

    @property
    def keyword(self):
        return " ".join((without_translation(self.title), *artist_names(self.artist)))


@dataclass(frozen=True)
class Candidate:
    track: Track
    lookup: object


def similarity(left, right):
    return SequenceMatcher(None, left, right, autojunk=False).ratio() if left and right else 0


def artist_script(name):
    letters = [letter for letter in name if letter.isalpha()]
    if letters and all(CJK.fullmatch(letter) for letter in letters):
        return "cjk"
    if letters and all(unicodedata.name(letter, "").startswith("LATIN ") for letter in letters):
        return "latin"
    return None


def candidate_score(track, candidate):
    if version_tags(track.title, track.album) != version_tags(candidate.title, candidate.album):
        return 0
    title = similarity(normalized(without_translation(track.title)),
                       normalized(without_translation(search_title(candidate.title))))
    if title < .78:
        return 0
    artists, others = artist_names(track.artist), artist_names(candidate.artist)
    if not artists or not others:
        return 0
    common = set(artists).intersection(others)
    scripts = {artist_script(artists[0]), artist_script(others[0])}
    gap = abs(track.duration - candidate.duration) if track.duration and candidate.duration else None
    album = None
    if track.album and candidate.album:
        local, remote = (re.sub(r"\s+(?:single|ep)$", "", normalized(value)) for value in (track.album, candidate.album))
        album = similarity(local, remote)
    if not common and scripts == {"latin", "cjk"}:
        # 跨文字的艺人名只能视为待确认别名，要求歌名和时长吻合，并核对可用的专辑证据。
        if title != 1 or gap is None or gap > (4 if album is not None else 2) or (album is not None and album < .90):
            return 0
        duration = 1 - max(0, gap - 1) / 9
        score = .75 * title + .25 * duration
        return min(HIGH_SCORE - 1, round(score * 100))
    if artists[0] == others[0] or {artists[0], others[0]}.issubset(common):
        artist = 1 if set(artists).issubset(others) or set(others).issubset(artists) else .94
    else:
        artist = similarity(artists[0], others[0])
    if artist < .72:
        return 0
    if gap is not None:
        if gap > 20:
            return 0
        duration = max(0, 1 - max(0, gap - 1) / 9)
        score = .50 * title + .30 * artist + .20 * duration
    else:
        score = .60 * title + .40 * artist
    if album is not None:
        score = .95 * score + .05 * album
    return round(score * 100)


def lyric_text(value):
    return value[:MAX_LYRIC] if isinstance(value, str) else ""


def lyrics_result(synced=None, plain=None, source=None, *, translated=None, timed=None, kind=None, duration=None):
    original = lyric_text(synced)
    plain = lyric_text(plain).strip()
    fallback = parse_lrc_lines(original)
    advanced = lyric_text(timed)
    timeline = []
    if advanced and kind in {"ttml", "lyricsfile"}:
        try:
            timeline = parse_ttml(advanced, duration) if kind == "ttml" else parse_lyricsfile(advanced)
        except (ParseError, ValueError, OverflowError, RecursionError, YAMLError):
            pass
    elif advanced and kind:
        timeline = parse_word_timing(advanced, kind)
        if kind == "krc":
            krc_translations(advanced, timeline)
    timeline = timeline or fallback
    if any(line.text for line in timeline):
        translation = lyric_text(translated)
        if translation:
            companion = parse_lrc_lines(translation) or parse_word_timing(
                translation, "yrc" if re.search(r"\(\d+,\d+,0\)", translation) else "qrc")
            align_translations(timeline, companion)
        # 旧客户端继续使用秒和纯文本；AMLL 单独接收完整毫秒时间轴。
        lines = [{"time": line.start / 1000, "text": line.text} for line in (fallback or timeline)]
        return {"status": "synced", "lines": lines, "plain": plain or None, "source": source,
                "amll_lines": amll_timeline(timeline)}
    if plain:
        return {"status": "plain", "lines": [], "plain": plain, "source": source}
    return None


async def get_json(client, url, *, params=None, referer=None, user_agent="Vitals/0.7 (self-hosted music dashboard)",
                   form=None, raw=False):
    headers = {"User-Agent": user_agent}
    if referer:
        headers["Referer"] = referer
    async with client.stream("POST" if form is not None else "GET", url, params=params, data=form, headers=headers) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("lyrics response too large")
    return body.decode("utf-8-sig") if raw else json.loads(body)


def rows(value):
    if not isinstance(value, list):
        raise ValueError("unexpected lyric search format")
    return [item for item in value[:20] if isinstance(item, dict)]


def artists_from(items):
    return ", ".join(display_text(item.get("name")) for item in items if isinstance(item, dict))


QQ_HOST = "https://c.y.qq.com"
QQ_REFERER = "https://y.qq.com/"
QQ_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
KUGOU_CATALOG = "https://mobileservice.kugou.com"
KUGOU_HOST = "https://lyrics.kugou.com"
NETEASE_HOST = "https://music.163.com"
LRCLIB = "https://lrclib.net"


async def search_qq(client, track):
    data = await get_json(client, QQ_HOST + "/soso/fcgi-bin/search_for_qq_cp",
                          params={"w": track.keyword, "format": "json", "p": 1, "n": 16, "t": 0,
                                  "remoteplace": "txt.yqq.song", "platform": "yqq.json"},
                          referer=QQ_REFERER, user_agent=QQ_USER_AGENT)
    hits = data["data"]["song"]["list"]
    return [Candidate(Track(display_text(item.get("songname")), artists_from(item.get("singer") or []),
                            display_text(item.get("albumname")), seconds(item.get("interval"))),
                      (item["songmid"], item.get("songid")))
            for item in rows(hits) if item.get("songmid")]


def decode_lrc(value):
    if not value:
        return ""
    return unescape(b64decode(value, validate=True).decode("utf-8-sig"))


def qrc_xml_text(raw):
    if not raw or not raw.lstrip().startswith("<"):
        return raw or ""
    xml_root(raw)
    # XML 属性的标准解析会把未转义的换行符折叠为空格，QRC 每行都有自己的行时间戳。
    match = re.search(r'\bLyricContent=(["\'])(.*?)\1', raw, re.S)
    return unescape(match[2]) if match else ""


def decode_qq_qrc(payload):
    # QQ 的外层响应有时缺少结束标签，固定提取只包含编码文字的 CDATA 曲轨。
    tracks = {}
    decoded = {}
    for kind, tag in (("original", "content"), ("translation", "contentts")):
        match = re.search(rf"<{tag}\b[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</{tag}>", payload, re.S)
        if match:
            text = match[1].strip()
            if text.startswith(("[", "<")):
                decoded[kind] = text
            elif re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
                tracks[kind] = "".join(text.split())
    executable = which("node")
    script = Path(__file__).resolve().parents[1] / "frontend" / "scripts" / "decode-qrc.mjs"
    if executable and script.is_file() and tracks:
        try:
            process = subprocess.run([executable, "--max-old-space-size=64", str(script)], input=json.dumps(tracks).encode(),
                                     capture_output=True, timeout=1.5, check=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            decoded.update(json.loads(process.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return qrc_xml_text(decoded.get("original")), qrc_xml_text(decoded.get("translation"))


async def fetch_qq_qrc(client, song_id):
    try:
        payload = await get_json(client, QQ_HOST + "/qqmusic/fcgi-bin/lyric_download.fcg", referer=QQ_REFERER,
                                 user_agent=QQ_USER_AGENT, raw=True,
                                 form={"version": "15", "miniversion": "100", "lrctype": "4", "musicid": song_id})
        return await asyncio.to_thread(decode_qq_qrc, payload)
    except (httpx.HTTPError, ParseError, OSError, ValueError, subprocess.SubprocessError):
        return "", ""


async def fetch_qq(client, candidate):
    mid, song_id = candidate.lookup
    data = await get_json(client, QQ_HOST + "/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                          params={"songmid": mid, "format": "json", "nobase64": 0},
                          referer=QQ_REFERER, user_agent=QQ_USER_AGENT)
    raw = decode_lrc(data.get("lyric"))
    plain = "\n".join(line for line in raw.splitlines() if not line.startswith("["))
    try:
        translation = decode_lrc(data.get("trans"))
    except (ValueError, TypeError):
        translation = ""
    qrc = lyric_text(data.get("qrc"))
    if not qrc and str(song_id).isdigit():
        try:
            qrc, translated_qrc = await asyncio.wait_for(fetch_qq_qrc(client, song_id), 2.2)
            translation = translated_qrc or translation
        except asyncio.TimeoutError:
            pass
    return lyrics_result(raw, plain, "qqmusic", translated=translation, timed=qrc, kind="qrc")


async def search_kugou(client, track):
    data = await get_json(client, KUGOU_CATALOG + "/api/v3/search/song",
                          params={"keyword": track.keyword, "format": "json", "page": 1, "pagesize": 16, "showtype": 1})
    return [Candidate(Track(display_text(item.get("songname")), display_text(item.get("singername")),
                            display_text(item.get("album_name")), seconds(item.get("duration"))), item["hash"])
            for item in rows(data["data"]["info"]) if item.get("hash")]


async def fetch_kugou(client, candidate):
    params = {"ver": 1, "client": "pc"}
    data = await get_json(client, KUGOU_HOST + "/search", params={**params, "man": "yes", "hash": candidate.lookup})
    entries = rows(data.get("candidates") or [])
    for entry in entries[:3]:
        if not entry.get("id") or not entry.get("accesskey"):
            continue
        download = {**params, "id": entry["id"], "accesskey": entry["accesskey"], "charset": "utf8"}
        try:
            richer = await asyncio.wait_for(get_json(client, KUGOU_HOST + "/download", params={**download, "fmt": "krc"}), 1.3)
            if richer.get("content"):
                result = lyrics_result(source="kugou", timed=decode_krc(richer["content"]), kind="krc")
                if result:
                    return result
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, ZlibError):
            pass
        lyric = await get_json(client, KUGOU_HOST + "/download", params={**download, "fmt": "lrc"})
        raw = decode_lrc(lyric.get("content"))
        if result := lyrics_result(raw, source="kugou"):
            return result
    return None


async def search_netease(client, track):
    data = await get_json(client, NETEASE_HOST + "/api/cloudsearch/pc",
                          params={"s": track.keyword, "type": 1, "limit": 16, "offset": 0}, referer=NETEASE_HOST + "/")
    if data.get("code") != 200:
        raise ValueError("NetEase search unavailable")
    candidates = []
    for item in rows((data.get("result") or {}).get("songs") or []):
        if item.get("id"):
            duration = seconds(item.get("dt"))
            identity = Track(display_text(item.get("name")), artists_from(item.get("ar") or []),
                             display_text((item.get("al") or {}).get("name")), duration / 1000 if duration else None)
            candidates.append(Candidate(identity, item["id"]))
    return candidates


async def fetch_netease(client, candidate):
    data = await get_json(client, NETEASE_HOST + "/api/song/lyric",
                          params={"id": candidate.lookup, "lv": 1, "tv": 1, "yv": 1, "ytv": 1},
                          referer=NETEASE_HOST + "/")
    lrc = (data.get("lrc") or {}).get("lyric")
    yrc = (data.get("yrc") or {}).get("lyric")
    translated = (data.get("ytlrc") or {}).get("lyric") or (data.get("tlyric") or {}).get("lyric")
    return lyrics_result(lrc, source="netease", translated=translated, timed=yrc, kind="yrc")


async def search_lrclib(client, track):
    params = {"track_name": without_translation(track.title)}
    if artists := artist_names(track.artist):
        params["artist_name"] = " ".join(artists)
    data = await get_json(client, LRCLIB + "/api/search", params=params)
    return [Candidate(Track(display_text(item.get("trackName")), display_text(item.get("artistName")),
                            display_text(item.get("albumName")), seconds(item.get("duration"))), item)
            for item in rows(data)]


async def fetch_lrclib(_client, candidate):
    record = candidate.lookup
    ttml = record.get("ttml")
    return lyrics_result(record.get("syncedLyrics"), record.get("plainLyrics"), "lrclib",
                         translated=record.get("translatedLyrics"), timed=ttml or record.get("lyricsfile"),
                         kind="ttml" if ttml else "lyricsfile")


async def search_amll(client, track):
    params = {"musicName": without_translation(track.title), "pageSize": 10}
    if artist_names(track.artist):
        params["artistName"] = artist_names(track.artist)[0]
    data = await get_json(client, AMLL_API + "/search", params=params)
    candidates = []
    for item in rows(data["data"]["items"]):
        identifier = item.get("id")
        if type(identifier) is not int or identifier < 0:
            continue
        names = [display_text(name) for name in item.get("musicNames") or []]
        artists = [display_text(name) for name in item.get("artistNames") or []]
        albums = [display_text(name) for name in item.get("albumNames") or []]
        # 搜索结果会列出名称别名，按本次搜索选择最接近的元数据组合。
        credits = sorted(artists, key=lambda name: similarity(normalized(name), normalized(track.artist)), reverse=True)
        variants = (Track(name, ", ".join(credits), album) for name in names[:8] for album in (albums[:8] or [""]))
        best = max(variants, key=lambda candidate: candidate_score(track, candidate), default=None)
        if best:
            candidates.append(Candidate(best, (identifier, track.duration)))
    return candidates


async def fetch_amll(client, candidate):
    identifier, duration = candidate.lookup
    data = await get_json(client, AMLL_API + "/get", params={"id": identifier})
    return lyrics_result(source="amll", timed=data["data"].get("lyrics"), kind="ttml", duration=duration)


SOURCES = ((search_qq, fetch_qq), (search_kugou, fetch_kugou), (search_netease, fetch_netease), (search_lrclib, fetch_lrclib))
PREFERRED_SOURCES = {
    "apple music": (search_amll, fetch_amll),
    "qq 音乐": SOURCES[0],
    "酷狗音乐": SOURCES[1],
    "网易云音乐": SOURCES[2],
}
SOURCE_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, asyncio.TimeoutError)


async def resolve_source(client, track, search, fetch):
    # 先用歌曲和艺人，其次仅主唱，最后仅歌名；所有候选仍与原曲身份比较。
    artists = artist_names(track.artist)
    queries = [track]
    if len(artists) > 1:
        queries.append(replace(track, artist=artists[0]))
    queries.append(replace(track, artist=""))
    failed = False
    seen = set()
    for query in queries:
        try:
            candidates = await search(client, query)
        except SOURCE_ERRORS:
            failed = True
            continue
        ranked = sorted(((candidate_score(track, candidate.track), candidate) for candidate in candidates),
                        key=lambda item: item[0], reverse=True)
        for score, candidate in ranked[:4]:
            if score < MIN_SCORE:
                break
            identifier = candidate.lookup.get("id") if isinstance(candidate.lookup, dict) else candidate.lookup
            if identifier is not None:
                if identifier in seen:
                    continue
                seen.add(identifier)
            try:
                result = await fetch(client, candidate)
            except SOURCE_ERRORS:
                failed = True
                continue
            if result:
                return (score, result), False
    return None, failed


async def find_lyrics(client, track, player=""):
    async def run(search, fetch):
        try:
            return await asyncio.wait_for(resolve_source(client, track, search, fetch), SOURCE_TIMEOUT)
        except SOURCE_ERRORS:
            return None, True

    preferred = PREFERRED_SOURCES.get(player)
    preferred_failed = False
    if preferred:
        match, preferred_failed = await run(*preferred)
        if match:
            return match[1]

    # gather 保留信任顺序，避免把网络速度误作选源优先级。
    outcomes = await asyncio.gather(*(run(search, fetch) for search, fetch in SOURCES if (search, fetch) != preferred))
    matches = [match for match, _ in outcomes if match]
    if matches:
        return next((result for score, result in matches if score >= HIGH_SCORE), matches[0][1])
    if preferred_failed or any(failed for _, failed in outcomes):
        return {**EMPTY_LYRICS, "status": "unavailable"}
    return EMPTY_LYRICS.copy()


class LyricsCache:
    def __init__(self, store):
        self.store = store
        self.locks = [asyncio.Lock() for _ in range(16)]

    def cached(self, key):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM media_lyrics WHERE cache_key = ? AND expires_at > ?", (key, time())).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, result, ttl):
        with self.store.connect() as db:
            db.execute("""INSERT INTO media_lyrics(cache_key, payload, expires_at) VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, expires_at = excluded.expires_at""",
                       (key, json.dumps(result, ensure_ascii=False), time() + ttl))
            db.execute("DELETE FROM media_lyrics WHERE expires_at <= ?", (time(),))

    async def get(self, client, track, refresh=False):
        identity = Track.from_state(track)
        if not normalized(identity.title) or not artist_names(identity.artist):
            return EMPTY_LYRICS.copy()
        player = display_text(track.get("player")).casefold()
        # 同一首歌的不同优先源分别缓存，通用播放器复用默认结果。
        preference = player if player in PREFERRED_SOURCES else ""
        key_data = ["multi-amll-v2", preference, identity.title.casefold(), identity.artist.casefold(), identity.album.casefold(),
                    round(identity.duration) if identity.duration else None]
        key = sha256(json.dumps(key_data).encode()).hexdigest()
        async with self.locks[int(key[0], 16)]:
            existing = await asyncio.to_thread(self.cached, key)
            if existing:
                retry_after = existing.pop("_retry_after", 0)
                if not refresh or existing.get("source") or time() < retry_after:
                    return existing
            result = await find_lyrics(client, identity, preference)
            ttl = {"missing": MISS_TTL, "unavailable": ERROR_TTL}.get(result["status"], 30 * 86400)
            cached = {**result, "_retry_after": time() + 6} if refresh and not result.get("source") else result
            await asyncio.to_thread(self.save, key, cached, ttl)
            return result
