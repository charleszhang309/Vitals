"""Tailnet 媒体 API、同用户桌面代理与歌词路由。"""

import asyncio
from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .agents import DEVICE_PATTERN
from .config import ROOT
from .lyrics import EMPTY_LYRICS, LyricsCache


ID_PATTERN = r"^[a-f0-9]{32}$"
ART_PATTERN = r"^[a-f0-9]{64}$"
MAX_ARTWORK = 512 * 1024
EMPTY_STATE = {"available": True, "active": False, "current_session_id": None, "sessions": []}
EMPTY_INPUT_VOLUME = {"input_percent": None, "input_muted": None, "input_device_id": None, "input_devices": []}
EMPTY_VOLUME = {"available": False, "percent": None, "muted": False, "device_id": None, "devices": [],
                **EMPTY_INPUT_VOLUME}
SessionId = Annotated[str, Field(pattern=ID_PATTERN)]
ArtworkVersion = Annotated[str, Field(pattern=ART_PATTERN)]


class MediaAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: SessionId
    action: Literal["play", "pause", "toggle", "next", "previous", "seek"]
    position_seconds: float | None = Field(default=None, strict=True, ge=0, le=86400, allow_inf_nan=False)
    track_id: ArtworkVersion | None = None

    @model_validator(mode="after")
    def validate_seek(self):
        if (self.action == "seek") != (self.position_seconds is not None):
            raise ValueError("仅 seek 需要 position_seconds")
        return self


class VolumeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percent: int | None = Field(default=None, strict=True, ge=0, le=100)
    muted: StrictBool | None = None
    device_id: ArtworkVersion | None = None
    input_percent: int | None = Field(default=None, strict=True, ge=0, le=100)
    input_muted: StrictBool | None = None
    input_device_id: ArtworkVersion | None = None

    @model_validator(mode="after")
    def validate_change(self):
        if all(value is None for value in (self.percent, self.muted, self.device_id,
                                           self.input_percent, self.input_muted, self.input_device_id)):
            raise ValueError("至少设置一项音频控制")
        return self


def desktop_connection_file():
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    install = sha256(str(ROOT).casefold().encode()).hexdigest()[:16]
    return local / "Vitals" / f"media-{install}.json"


class MediaBridge:
    """只访问回环地址。主服务不需要在交互式 Windows 会话中运行。"""

    artwork_content_type = "image/webp"

    def __init__(self):
        self.path = desktop_connection_file()

    def endpoint(self):
        with self.path.open(encoding="utf-8") as file:
            value = json.loads(file.read(512))
        port, token = value["port"], value["token"]
        if type(port) is not int or not 0 < port <= 65535 or not re.fullmatch(ART_PATTERN, token):
            raise ValueError("invalid desktop endpoint")
        return port, token

    async def call(self, client, method, path, **kwargs):
        if client is None or os.name != "nt":
            return None
        try:
            port, token = await asyncio.to_thread(self.endpoint)
            return await client.request(method, f"http://127.0.0.1:{port}{path}",
                                        headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
            return None

    async def state(self, client):
        response = await self.call(client, "GET", "/state")
        if response is not None and response.is_success:
            with suppress(ValueError):
                return response.json()
        return {**EMPTY_STATE, "reason": "desktop_unavailable"}

    async def volume(self, client):
        response = await self.call(client, "GET", "/volume")
        if response is not None and response.is_success:
            with suppress(ValueError):
                return response.json()
        return EMPTY_VOLUME.copy()

    async def action(self, client, action):
        response = await self.call(client, "POST", "/action", json=action.model_dump(exclude_none=True))
        return self.command_result(response)

    async def set_volume(self, client, change):
        response = await self.call(client, "POST", "/volume", json=change.model_dump(exclude_none=True))
        return self.command_result(response)

    @staticmethod
    def command_result(response):
        if response is not None:
            if response.is_success:
                with suppress(ValueError):
                    return response.json()
            elif response.status_code in {404, 409, 422, 503}:
                with suppress(ValueError, KeyError, TypeError):
                    detail = response.json()["detail"]
                    if isinstance(detail, str):
                        raise HTTPException(response.status_code, detail)
        raise HTTPException(503, "当前无法连接 Windows 桌面媒体服务")

    async def artwork(self, client, session_id, version):
        response = await self.call(client, "GET", "/artwork", params={"session_id": session_id, "version": version})
        if response is not None and response.is_success and len(response.content) <= MAX_ARTWORK:
            return response.content
        return None


class MacMediaBridge(MediaBridge):
    artwork_content_type = "image/jpeg"

    def __init__(self):
        from mac_agent.playback import Playback

        self.playback = Playback()
        self.lock = Lock()
        self.last_state = None
        self.sampled_at = 0

    def _state(self):
        if self.last_state is not None and monotonic() - self.sampled_at < 0.5:
            return self.last_state
        from mac_agent.macos import read_optional

        media = read_optional("desktop media", self.playback.sample)
        if media is None:
            self.last_state = {**EMPTY_STATE, "reason": "no_media"}
        else:
            sid = sha256(f"macos:{media['player']}".encode()).hexdigest()[:32]
            actions = {"play": True, "pause": True, "toggle": True, "next": True, "previous": True,
                       "seek": media["position_seconds"] is not None and bool(media["duration_seconds"])}
            self.last_state = {"available": True, "active": media["status"] == "playing", "current_session_id": sid,
                               "sessions": [{**media, "session_id": sid, "playback_rate": 1, "actions": actions}]}
        self.sampled_at = monotonic()
        return self.last_state

    def _read_state(self):
        with self.lock:
            return self._state()

    async def state(self, client):
        return await asyncio.to_thread(self._read_state)

    def _artwork(self, session_id, version):
        with self.lock:
            if self._state()["current_session_id"] != session_id:
                return None
            return self.playback.bytes_for(version)

    async def artwork(self, client, session_id, version):
        return await asyncio.to_thread(self._artwork, session_id, version)

    def _action(self, change):
        from mac_agent.native import javascript
        from mac_agent.playback import MEDIA_SCRIPT

        verbs = {"play": "play", "pause": "pause", "toggle": "playpause", "next": "nextTrack",
                 "previous": "previousTrack", "seek": "seek"}
        with self.lock:
            track = next(iter(self._state()["sessions"]), None)
            if not track or track["session_id"] != change.session_id:
                raise HTTPException(404, "找不到播放器会话")
            if change.track_id and change.track_id != track["track_id"]:
                raise HTTPException(409, "当前曲目已变化")
            if not track["actions"][change.action]:
                raise HTTPException(409, "播放器未提供该操作")
            player = "music" if track["player"] == "Apple Music" else "spotify"
            args = [verbs[change.action], player]
            if change.position_seconds is not None:
                args.append(str(min(change.position_seconds, track["duration_seconds"])))
            javascript(MEDIA_SCRIPT, *args)
            self.last_state = None
            return {"accepted": True}

    async def action(self, client, change):
        try:
            return await asyncio.to_thread(self._action, change)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise HTTPException(503, "macOS 播放器控制暂不可用") from error

    @staticmethod
    def _audio_ids(data, key):
        return {sha256(device["id"].encode()).hexdigest(): device for device in data.get(key, [])}

    @classmethod
    def _public_volume(cls, data):
        if data is None:
            return EMPTY_VOLUME.copy()
        result = {**EMPTY_VOLUME, **data}
        result["available"] = bool(result["devices"]) or (result["percent"] is not None and result["muted"] is not None)
        for device_id, devices in (("device_id", "devices"), ("input_device_id", "input_devices")):
            current = result[device_id]
            result[device_id] = sha256(current.encode()).hexdigest() if current else None
            result[devices] = [{"id": key, "name": item["name"]} for key, item in cls._audio_ids(data, devices).items()]
        return result

    async def volume(self, client):
        from mac_agent.macos import read_optional, volume

        return self._public_volume(await asyncio.to_thread(read_optional, "desktop volume", volume))

    def _set_volume(self, change):
        from mac_agent.macos import execute_command, volume

        with self.lock:
            if change.device_id or change.input_device_id:
                current = volume()
                switches = []
                for device_id, devices, action, label in (
                    (change.device_id, "devices", "audio_device_set", "输出"),
                    (change.input_device_id, "input_devices", "input_audio_device_set", "输入"),
                ):
                    if device_id is not None:
                        device = self._audio_ids(current or {}, devices).get(device_id)
                        if not device:
                            raise HTTPException(404, f"找不到该音频{label}设备")
                        switches.append((action, device["id"]))
                for action, device_id in switches:
                    execute_command(action, {"device_id": device_id})
            for action, field, value in (
                ("volume_set", "percent", change.percent), ("volume_mute", "muted", change.muted),
                ("input_volume_set", "percent", change.input_percent), ("input_volume_mute", "muted", change.input_muted),
            ):
                if value is not None:
                    execute_command(action, {field: value})
            return self._public_volume(volume())

    async def set_volume(self, client, change):
        try:
            return await asyncio.to_thread(self._set_volume, change)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            raise HTTPException(503, "macOS 音频设备暂不可用") from error


def media_router(bridge, lyrics: LyricsCache, agents=None):
    router = APIRouter(prefix="/api/media", tags=["media"])

    def local(request):
        return getattr(request.app.state, "media_client", None)

    async def selected(request, session_id):
        state = await bridge.state(local(request))
        target = session_id or state["current_session_id"]
        return next((item for item in state["sessions"] if item["session_id"] == target), None)

    @router.get("/state")
    async def state(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await bridge.state(local(request))

    @router.post("/action")
    async def action(request: Request, change: MediaAction):
        return await bridge.action(local(request), change)

    @router.get("/volume")
    async def volume(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {**EMPTY_INPUT_VOLUME, **await bridge.volume(local(request))}

    @router.post("/volume")
    async def change_volume(request: Request, change: VolumeChange):
        return {**EMPTY_INPUT_VOLUME, **await bridge.set_volume(local(request), change)}

    @router.get("/artwork")
    async def artwork(request: Request, session_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
                      version: Annotated[str | None, Query(pattern=ART_PATTERN)] = None):
        item = await selected(request, session_id)
        digest = item and item.get("artwork_version")
        if not digest or (version and version != digest):
            raise HTTPException(404, "该会话暂无封面")
        etag = f'"{digest}"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=86400, immutable" if version else "private, no-cache",
                   "X-Content-Type-Options": "nosniff"}
        if any(tag.strip() in {etag, "*"} for tag in request.headers.get("if-none-match", "").split(",")):
            return Response(status_code=304, headers=headers)
        contents = await bridge.artwork(local(request), item["session_id"], digest)
        if contents is None:
            raise HTTPException(404, "该会话暂无封面")
        return Response(contents, media_type=bridge.artwork_content_type, headers=headers)

    @router.get("/lyrics")
    async def track_lyrics(request: Request, response: Response,
                           session_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
                           track_id: Annotated[str | None, Query(pattern=ART_PATTERN)] = None,
                           refresh: bool = False,
                           agent_id: Annotated[str | None, Query(pattern=DEVICE_PATTERN)] = None):
        response.headers["Cache-Control"] = "no-store"
        track = (agents.current_media(agent_id) if agents else None) if agent_id else await selected(request, session_id)
        if not track or (track_id and track["track_id"] != track_id):
            return EMPTY_LYRICS.copy()
        client = getattr(request.app.state, "external_client", None)
        return await lyrics.get(client, track, refresh=refresh) if client else {**EMPTY_LYRICS, "status": "unavailable"}

    return router
