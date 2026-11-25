"""Tailnet 媒体 API、同用户桌面代理与歌词路由。"""

import asyncio
from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .config import ROOT
from .lyrics import EMPTY_LYRICS, LyricsCache


ID_PATTERN = r"^[a-f0-9]{32}$"
ART_PATTERN = r"^[a-f0-9]{64}$"
MAX_ARTWORK = 512 * 1024
EMPTY_STATE = {"available": True, "active": False, "current_session_id": None, "sessions": []}
EMPTY_VOLUME = {"available": False, "percent": None, "muted": False, "device_id": None, "devices": []}
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

    @model_validator(mode="after")
    def validate_change(self):
        if self.percent is None and self.muted is None and self.device_id is None:
            raise ValueError("至少设置音量、静音状态或输出设备中的一项")
        return self


def desktop_connection_file():
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    install = sha256(str(ROOT).casefold().encode()).hexdigest()[:16]
    return local / "Vitals" / f"media-{install}.json"


class MediaBridge:
    """只访问回环地址。主服务不需要在交互式 Windows 会话中运行。"""

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


def media_router(bridge, lyrics: LyricsCache):
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
        return await bridge.volume(local(request))

    @router.post("/volume")
    async def change_volume(request: Request, change: VolumeChange):
        return await bridge.set_volume(local(request), change)

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
        return Response(contents, media_type="image/webp", headers=headers)

    @router.get("/lyrics")
    async def track_lyrics(request: Request, response: Response,
                           session_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
                           track_id: Annotated[str | None, Query(pattern=ART_PATTERN)] = None,
                           refresh: bool = False):
        response.headers["Cache-Control"] = "no-store"
        track = await selected(request, session_id)
        if not track or (track_id and track["track_id"] != track_id):
            return EMPTY_LYRICS.copy()
        client = getattr(request.app.state, "external_client", None)
        return await lyrics.get(client, track, refresh=refresh) if client else {**EMPTY_LYRICS, "status": "unavailable"}

    return router
