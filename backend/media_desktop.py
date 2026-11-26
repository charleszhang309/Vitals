"""Windows 交互会话内的 SMTC / Core Audio 实现。"""

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
import json
import math
import os
from pathlib import Path
from secrets import token_hex
import socket
import sys
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from PIL import Image
from starlette.concurrency import run_in_threadpool
import uvicorn

from .media import (EMPTY_STATE, EMPTY_VOLUME, MAX_ARTWORK, ArtworkVersion, MediaAction, SessionId,
                    VolumeChange, desktop_connection_file)


PLAYER_NAMES = {
    "applemusic": "Apple Music", "apple.music": "Apple Music", "itunes": "iTunes", "qqmusic": "QQ 音乐",
    "tencentmusic": "QQ 音乐", "cloudmusic": "网易云音乐", "netease": "网易云音乐", "spotify": "Spotify",
    "kugou": "酷狗音乐", "酷狗": "酷狗音乐",
    "chrome": "Chrome", "msedge": "Edge", "microsoftedge": "Edge", "firefox": "Firefox",
    "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi",
}
ACTIONS = {
    "play": ("is_play_enabled", "try_play_async"), "pause": ("is_pause_enabled", "try_pause_async"),
    "toggle": ("is_play_pause_toggle_enabled", "try_toggle_play_pause_async"),
    "next": ("is_next_enabled", "try_skip_next_async"), "previous": ("is_previous_enabled", "try_skip_previous_async"),
    "seek": ("is_playback_position_enabled", "try_change_playback_position_async"),
}
NATIVE_ERRORS = (OSError, RuntimeError, ValueError, TypeError, AttributeError, asyncio.TimeoutError, ImportError)


def player_name(app_id):
    folded = app_id.casefold()
    return next((name for fragment, name in PLAYER_NAMES.items() if fragment in folded),
                app_id.replace("\\", "/").rsplit("/", 1)[-1].split("!", 1)[0].removesuffix(".exe")[:80] or "Windows 媒体")


def playback_snapshot(info, timeline, now):
    status = {4: "playing", 5: "paused"}.get(int(info.playback_status), "stopped")
    rate = info.playback_rate
    rate = rate if isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0 else 1
    start = timeline.start_time.total_seconds()
    duration = timeline.end_time.total_seconds() - start
    duration = duration if duration > 0 else None
    position = timeline.position.total_seconds() - start
    updated = timeline.last_updated_time
    if status == "playing" and updated and updated.year > 1970 and duration is not None:
        position += max(0, (now - updated.replace(tzinfo=updated.tzinfo or timezone.utc)).total_seconds()) * rate
    position = max(0, min(position, duration) if duration is not None else position)
    enabled = {key: bool(getattr(info.controls, flag, False)) for key, (flag, _) in ACTIONS.items()}
    enabled["seek"] = enabled["seek"] and duration is not None
    return {"status": status, "duration_seconds": duration, "position_seconds": position, "playback_rate": rate,
            "updated_at": now.isoformat(timespec="milliseconds"), "actions": enabled}


def convert_artwork(raw):
    target = BytesIO()
    with Image.open(BytesIO(raw)) as img:
        if img.width * img.height > 16_000_000:
            return None
        img.thumbnail((432, 432))
        img.convert("RGB").save(target, "WEBP", quality=84)
    return target.getvalue() if target.tell() <= MAX_ARTWORK else None


async def artwork_bytes(thumbnail):
    from winrt.windows.storage.streams import DataReader

    with await thumbnail.open_read_async() as stream:
        size = stream.size
        if not 0 < size <= 2 * 1024 * 1024:
            return None
        buffer = bytearray(size)
        with DataReader(stream.get_input_stream_at(0)) as reader:
            if await reader.load_async(size) != size:
                return None
            reader.read_bytes(buffer)
    return await asyncio.to_thread(convert_artwork, buffer)


async def wait_native(operation, timeout):
    # 超时后释放代理锁，避免有问题的播放器让整台主机的媒体接口一直等待。
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    finally:
        if not task.done():
            task.cancel()


@dataclass
class Entry:
    session: object
    app_id: str
    id: str = field(default_factory=lambda: uuid4().hex)
    title: str = ""
    artist: str = ""
    album: str = ""
    track_id: str | None = None
    artwork: bytes | None = None
    artwork_version: str | None = None
    artwork_retry_at: float = 0


class WindowsMedia:
    def __init__(self):
        self.manager = None
        self.entries = []
        self.lock = asyncio.Lock()
        self.latest = {**EMPTY_STATE, "reason": "desktop_unavailable"}
        self.refresh_after = 0.0

    async def manager_for_desktop(self):
        if self.manager is None:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

            self.manager = await wait_native(GlobalSystemMediaTransportControlsSessionManager.request_async(), 1.2)
        return self.manager

    @staticmethod
    async def describe(entry):
        session = entry.session
        try:
            play = playback_snapshot(session.get_playback_info(), session.get_timeline_properties(), datetime.now(timezone.utc))
        except NATIVE_ERRORS:
            return None
        with suppress(*NATIVE_ERRORS):
            props = await wait_native(session.try_get_media_properties_async(), 0.65)
            names = tuple((getattr(props, key, "") or "").strip()[:400] for key in ("title", "artist", "album_title"))
            track_id = sha256(json.dumps(names, ensure_ascii=False).encode()).hexdigest()
            if track_id != entry.track_id:
                entry.title, entry.artist, entry.album = names
                entry.track_id = track_id
                entry.artwork = entry.artwork_version = None
                entry.artwork_retry_at = 0
            if entry.artwork is None and monotonic() >= entry.artwork_retry_at:
                # 部分播放器先发布曲名，再异步提供封面。
                thumbnail = props.thumbnail
                if thumbnail:
                    entry.artwork_retry_at = monotonic() + 3
                    with suppress(*NATIVE_ERRORS, Image.DecompressionBombError):
                        entry.artwork = await wait_native(artwork_bytes(thumbnail), 0.75)
                        if entry.artwork:
                            entry.artwork_version = sha256(entry.artwork).hexdigest()
        return {"session_id": entry.id, "app_id": entry.app_id, "player": player_name(entry.app_id),
                "title": entry.title, "artist": entry.artist, "album": entry.album, "track_id": entry.track_id,
                "has_artwork": entry.artwork is not None, "artwork_version": entry.artwork_version, **play}

    async def refresh(self):
        try:
            manager = await self.manager_for_desktop()
            native = list(manager.get_sessions())[:16]
            current = manager.get_current_session()
            apps = [session.source_app_user_model_id for session in native]
            entries = []
            for session, app_id in zip(native, apps):
                match = next((item for item in self.entries if item.session == session), None)
                if match is None and apps.count(app_id) == 1:
                    candidates = [item for item in self.entries if item.app_id == app_id]
                    match = candidates[0] if len(candidates) == 1 else None
                entry = match or Entry(session, app_id)
                entry.session = session
                entries.append(entry)
            self.entries = entries
            views = [item for item in await asyncio.gather(*(self.describe(entry) for entry in entries)) if item is not None]
            active = next((item.id for item in entries if item.session == current), None)
            if current is not None and active is None and apps.count(current.source_app_user_model_id) == 1:
                active = entries[apps.index(current.source_app_user_model_id)].id
            fallback = next((item for item in views if item["status"] == "playing"), views[0] if views else None)
            selected = next((item for item in views if item["session_id"] == active), fallback)
            self.latest = {"available": True, "active": bool(views), "sessions": views,
                           "current_session_id": selected["session_id"] if selected else None,
                           "reason": None if views else "no_session"}
        except NATIVE_ERRORS:
            self.manager = None
            self.entries = []
            self.latest = {**EMPTY_STATE, "reason": "media_unavailable"}
        self.refresh_after = monotonic() + 0.7

    async def state(self):
        async with self.lock:
            if monotonic() >= self.refresh_after:
                await self.refresh()
            return self.latest

    async def artwork(self, session_id, version):
        async with self.lock:
            return next((item.artwork for item in self.entries if item.id == session_id and item.artwork_version == version), None)

    async def act(self, change):
        async with self.lock:
            # 动作前重新枚举，已经消失的会话不能回退到另一个应用。
            await self.refresh()
            entry = next((item for item in self.entries if item.id == change.session_id), None)
            view = next((item for item in self.latest["sessions"] if item["session_id"] == change.session_id), None)
            if entry is None or view is None:
                raise HTTPException(404, "该播放器会话已结束")
            if change.track_id and change.track_id != view["track_id"]:
                raise HTTPException(409, "曲目已变更，请重试")
            if not view["actions"][change.action]:
                raise HTTPException(409, "此播放器不支持该动作")
            arguments = []
            try:
                if change.action == "seek":
                    timeline = entry.session.get_timeline_properties()
                    target = timeline.start_time.total_seconds() + min(change.position_seconds, view["duration_seconds"])
                    low, high = timeline.min_seek_time.total_seconds(), timeline.max_seek_time.total_seconds()
                    target = max(low, min(target, high)) if high > low else target
                    arguments = [round(target * 10_000_000)]
                execute = getattr(entry.session, ACTIONS[change.action][1])
                accepted = await wait_native(execute(*arguments), 1.1)
            except NATIVE_ERRORS:
                raise HTTPException(503, "Windows 媒体会话未响应") from None
            if not accepted:
                raise HTTPException(409, "播放器未接受该动作")
            self.refresh_after = 0
            return {"accepted": True}


class WindowsAudio:
    def __init__(self):
        # 在主线程首次导入，工作线程每次操作独立配对初始化和释放 COM。
        self.lock = Lock()
        try:
            from comtypes import COMError, CoInitialize, CoUninitialize
            from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
            from pycaw.pycaw import AudioUtilities
        except NATIVE_ERRORS:
            self.speakers = None
        else:
            self.endpoint_errors = (OSError, COMError)
            self.audio_errors = NATIVE_ERRORS + (COMError,)
            self.initialize, self.uninitialize, self.speakers = CoInitialize, CoUninitialize, AudioUtilities.GetSpeakers
            self.outputs = lambda: AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
            self.switch = lambda device_id: AudioUtilities.SetDefaultDevice(device_id, [ERole.eConsole, ERole.eMultimedia])

    @staticmethod
    def device_key(device):
        return sha256(device.id.encode()).hexdigest()

    def current_volume(self, change):
        devices = {self.device_key(device): device for device in self.outputs()}
        active_outputs = [{"id": key, "name": device.FriendlyName or "音频输出"} for key, device in devices.items()]
        current = None
        if devices:
            with suppress(*self.endpoint_errors):
                current = self.speakers()
        if change is not None and change.device_id is not None:
            target = devices.get(change.device_id)
            if target is None:
                raise HTTPException(409, "该输出设备已断开或不可用")
            if current is None or self.device_key(current) != change.device_id:
                self.switch(target.id)
                current = self.speakers()
                if current is None or self.device_key(current) != change.device_id:
                    raise HTTPException(409, "Windows 尚未确认输出设备切换")
        if current is None:
            if change is not None:
                raise HTTPException(503, "Windows 默认音频设备不可用")
            return {**EMPTY_VOLUME, "devices": active_outputs}
        endpoint = current.EndpointVolume
        if change is not None and change.percent is not None:
            endpoint.SetMasterVolumeLevelScalar(change.percent / 100, None)
        if change is not None and change.muted is not None:
            endpoint.SetMute(int(change.muted), None)
        return {"available": True, "percent": round(endpoint.GetMasterVolumeLevelScalar() * 100),
                "muted": bool(endpoint.GetMute()), "device_id": self.device_key(current), "devices": active_outputs}

    def volume(self, change=None):
        if self.speakers is None:
            if change is not None:
                raise HTTPException(503, "Windows 音频服务不可用")
            return EMPTY_VOLUME.copy()
        try:
            self.initialize()
            try:
                with self.lock:
                    return self.current_volume(change)
            finally:
                self.uninitialize()
        except self.audio_errors:
            if change is not None:
                raise HTTPException(503, "Windows 默认音频设备不可用") from None
            return EMPTY_VOLUME.copy()


def desktop_app(token, media, audio):
    def authorize(authorization: str = Header(default="")):
        if not compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise HTTPException(403)

    app = FastAPI(dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route("/state", media.state, methods=["GET"])

    @app.post("/action")
    async def action(change: MediaAction):
        return await media.act(change)

    @app.get("/volume")
    def volume():
        return audio.volume()

    @app.post("/volume")
    def set_volume(change: VolumeChange):
        return audio.volume(change)

    @app.get("/artwork")
    async def artwork(session_id: SessionId, version: ArtworkVersion):
        image = await media.artwork(session_id, version)
        if image is None:
            raise HTTPException(404)
        return Response(image, media_type="image/webp")

    return app


@contextmanager
def media_apartment():
    # asyncio 不泵送 STA 消息，SMTC 和 comtypes 的初始公寓必须为 MTA。
    sys.coinit_flags = 0
    uninit = None
    try:
        with suppress(ImportError, OSError):
            from winrt.runtime import ApartmentType, init_apartment, uninit_apartment

            init_apartment(ApartmentType.MULTI_THREADED)
            uninit = uninit_apartment
        yield
    finally:
        if uninit is not None:
            uninit()


class InteractiveMediaBridge:
    """The tray starts the hub in the signed-in desktop session."""

    artwork_content_type = "image/webp"

    def __init__(self):
        self.player = WindowsMedia()
        self.audio = None

    @contextmanager
    def session(self):
        with media_apartment():
            self.audio = WindowsAudio()
            try:
                yield
            finally:
                self.audio = None

    async def state(self, client):
        return await self.player.state()

    async def action(self, client, change):
        return await self.player.act(change)

    async def volume(self, client):
        return await run_in_threadpool(self.audio.volume)

    async def set_volume(self, client, change):
        return await run_in_threadpool(self.audio.volume, change)

    async def artwork(self, client, session_id, version):
        return await self.player.artwork(session_id, version)


def main():
    from ctypes import WinDLL, get_last_error, wintypes

    path = desktop_connection_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    kernel = WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    mutex = kernel.CreateMutexW(None, False, "Global\\VitalsMedia-" + sha256(str(path).casefold().encode()).hexdigest())
    code = get_last_error()
    if not mutex:
        raise OSError(code, "could not reserve desktop media bridge")
    try:
        if code == 183:
            return
        token = token_hex(32)
        with media_apartment(), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            pending = None
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".media-", delete=False) as manifest:
                    pending = Path(manifest.name)
                    json.dump({"port": port, "token": token}, manifest)
                pending.replace(path)
                config = uvicorn.Config(desktop_app(token, WindowsMedia(), WindowsAudio()), log_config=None,
                                        access_log=False, proxy_headers=False, ws="none")
                uvicorn.Server(config).run(sockets=[listener])
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__" and os.name == "nt":
    main()
