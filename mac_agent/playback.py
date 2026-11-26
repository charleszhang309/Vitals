"""Read the active desktop player and cache its resized artwork."""

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import logging
from math import isfinite
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .native import apple, javascript

logger = logging.getLogger(__name__)
MEDIA_ACTIONS = {"media_play_pause": "playpause", "media_next": "nextTrack", "media_previous": "previousTrack"}
MAX_SOURCE_BYTES = 10 * 1024 * 1024
SPOTIFY_ARTWORK_HOSTS = {"i.scdn.co", "mosaic.scdn.co", "image-cdn-ak.spotifycdn.com", "image-cdn-fa.spotifycdn.com"}
MEDIA_SCRIPT = """
function read(getter, fallback) {
    try { return getter() ?? fallback; } catch (_) { return fallback; }
}
function run(args) {
    const players = [
        ["com.apple.Music", "Apple Music", "music"],
        ["com.spotify.client", "Spotify", "spotify"]
    ].flatMap(([id, player, source]) => {
        const app = read(() => Application(id), null);
        if (!app || !app.running()) return [];
        const status = read(() => app.playerState(), "unknown");
        if (status === "unknown") return [];
        return [{app, player, source, status}];
    });
    const target = players.find(p => p.status === "playing") ||
        players.find(p => p.status === "paused") || players[0];
    if (args.length) {
        if (!target || !target.app.running()) throw new Error("Music / Spotify is not available");
        switch (args[0]) {
            case "playpause": target.app.playpause(); break;
            case "nextTrack": target.app.nextTrack(); break;
            case "previousTrack": target.app.previousTrack(); break;
            default: throw new Error("Unknown media action");
        }
        return;
    }
    if (!target) return "null";
    const {app, player, source, status} = target;
    const result = {player, source, status};
    if (status === "playing" || status === "paused") {
        const track = app.currentTrack;
        result.title = read(() => track.name(), "");
        result.artist = read(() => track.artist(), "");
        result.album = read(() => track.album(), "");
        result.identity = read(() => source === "music" ? track.persistentID() : track.id(), "");
        result.position = read(() => app.playerPosition(), null);
        result.duration = read(() => track.duration(), null);
        if (source === "spotify") result.artwork_url = read(() => track.artworkUrl(), "");
    }
    return JSON.stringify(result);
}
"""
MUSIC_ARTWORK = """
on run argv
    if application id "com.apple.Music" is not running then return
    tell application id "com.apple.Music"
        set theTrack to current track
        if persistent ID of theTrack is not item 1 of argv then return
        if (count of artworks of theTrack) is 0 then return
        set coverData to raw data of artwork 1 of theTrack
        if coverData is missing value then return
        if persistent ID of current track is not item 1 of argv then return
    end tell
    set handle to open for access (POSIX file (item 2 of argv)) with write permission
    try
        write coverData to handle
        close access handle
    on error messageText number errorNumber
        close access handle
        error messageText number errorNumber
    end try
end run
"""


def seconds(value, *, scale=1):
    return round(value / scale, 3) if type(value) in (int, float) and isfinite(value) and value >= 0 else None


def normalize_track(raw):
    track = {key: (raw.get(key) or "")[:300] for key in ("title", "artist", "album")}
    identity = raw.get("identity") or [track["title"], track["artist"], track["album"], raw.get("duration")]
    track["track_id"] = sha256(json.dumps([raw["source"], identity]).encode()).hexdigest() if track["title"] else None
    duration = seconds(raw.get("duration"), scale=1000 if raw["source"] == "spotify" else 1)
    position = seconds(raw.get("position"))
    if position is not None and duration:
        position = min(position, duration)
    track.update(player=raw["player"], position_seconds=position, duration_seconds=duration,
                 status=raw["status"] if raw["status"] in {"playing", "paused", "stopped"} else "unknown",
                 updated_at=datetime.now(timezone.utc).isoformat(), artwork_version=None)
    return track


def spotify_artwork(url):
    destination = urlsplit(url)
    if (destination.scheme != "https" or destination.hostname not in SPOTIFY_ARTWORK_HOSTS
            or destination.username is not None or destination.port not in (None, 443)):
        return None
    # The player's URL is a separate origin. Nothing from the Vitals session is sent.
    with httpx.stream("GET", url, timeout=2, trust_env=False) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_SOURCE_BYTES:
                return None
        return bytes(data)


def extract_artwork(raw, allow_download):
    if raw["source"] == "music" and raw.get("identity"):
        with TemporaryDirectory(prefix="vitals-cover-") as directory:
            target = Path(directory) / "cover"
            apple(MUSIC_ARTWORK, raw["identity"], str(target))
            if not target.is_file() or target.stat().st_size > MAX_SOURCE_BYTES:
                return None
            data = target.read_bytes()
    elif allow_download and raw.get("artwork_url"):
        data = spotify_artwork(raw["artwork_url"])
    else:
        return None
    if not data:
        return None

    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            return None
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=82, optimize=True)
        return buffer.getvalue()


class Playback:
    def __init__(self, *, allow_download=True):
        self.allow_download = allow_download
        self.track_id = self.artwork_version = self.artwork = None

    def sample(self):
        raw = json.loads(javascript(MEDIA_SCRIPT))
        if raw is None:
            self.track_id = self.artwork_version = self.artwork = None
            return None
        track = normalize_track(raw)
        if track["track_id"] != self.track_id:
            self.track_id = track["track_id"]
            self.artwork_version = self.artwork = None
            if self.track_id:
                try:
                    self.artwork = extract_artwork(raw, self.allow_download)
                except (httpx.HTTPError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired,
                        Image.DecompressionBombError) as error:
                    logger.debug("Media artwork unavailable (%s)", type(error).__name__)
                if self.artwork:
                    self.artwork_version = sha256(self.track_id.encode() + self.artwork).hexdigest()
        track["artwork_version"] = self.artwork_version
        return track

    def bytes_for(self, version):
        return self.artwork if version is not None and version == self.artwork_version else None
