"""远程设备快照、一次性指令下发及共享 Agent 鉴权。"""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
import json
import logging
import re
import sqlite3
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, IPvAnyAddress, StrictBool, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .lyrics import metadata_track_id


OFFLINE_AFTER = 15
PROFILE_INTERVAL = 15
RESULT_TIMEOUT = 60
KEEPALIVE_SECONDS = 20
STREAM_FALLBACK_DELAY = 0.75
MAX_DEVICES = 32
MAX_COMMANDS = 32
DEVICE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
HASH_PATTERN = r"^[a-f0-9]{64}$"
MAX_ARTWORK = 512 * 1024
HUB_DEVICE_ID = "hub"
logger = logging.getLogger(__name__)
DeviceId = Annotated[str, Path(pattern=DEVICE_PATTERN)]
Digest = Annotated[str, StringConstraints(pattern=HASH_PATTERN)]
DeviceUID = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Bytes = Annotated[int, Field(ge=0, strict=True)]
Speed = Annotated[float, Field(ge=0, allow_inf_nan=False)]
bearer = HTTPBearer(auto_error=False)


def require_agent_token(config):
    expected = config.token.get_secret_value() if config.token else ""

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not expected:
            raise HTTPException(503, "尚未配置 Agent token")
        if credentials is None or not compare_digest(credentials.credentials.encode(), expected.encode()):
            raise HTTPException(401, "Agent token 无效", headers={"WWW-Authenticate": "Bearer"})

    return authenticate


class Battery(BaseModel):
    percent: Percent
    charging: StrictBool
    state: Literal["charging", "charged", "discharging", "not_charging", "unknown"] | None = None
    minutes_left: Annotated[int, Field(ge=0, strict=True)] | None = None
    cycle_count: Bytes | None = None
    health_percent: Percent | None = None
    temperature_c: Annotated[float, Field(ge=-50, le=100, allow_inf_nan=False)] | None = None


class CPU(BaseModel):
    usage_percent: Percent
    name: Label | None = None
    physical_cores: Bytes | None = None
    logical_cores: Bytes | None = None


class Memory(BaseModel):
    used_bytes: Bytes
    total_bytes: Bytes
    percent: Percent
    available_bytes: Bytes | None = None


class Disk(BaseModel):
    mountpoint: Label
    name: Label | None = None
    free_bytes: Bytes
    total_bytes: Bytes


class NetworkInterface(BaseModel):
    name: Label
    addresses: list[IPvAnyAddress] = Field(max_length=16)
    tailscale: StrictBool = False


class NetworkInfo(BaseModel):
    interfaces: list[NetworkInterface] = Field(default_factory=list, max_length=32)


class Network(NetworkInfo):
    upload_bytes_per_sec: Speed | None = None
    download_bytes_per_sec: Speed | None = None
    total_upload_bytes: Bytes | None = None
    total_download_bytes: Bytes | None = None


class Process(BaseModel):
    name: Label
    rss_bytes: Bytes
    pid: Annotated[int, Field(ge=0, strict=True)] | None = None


class AudioDevice(BaseModel):
    id: DeviceUID
    name: Label


class Volume(BaseModel):
    percent: Percent | None = None
    muted: StrictBool | None = None
    device_id: DeviceUID | None = None
    devices: list[AudioDevice] = Field(default_factory=list, max_length=64)


class Media(BaseModel):
    title: str = Field(default="", max_length=300)
    artist: str = Field(default="", max_length=300)
    status: Literal["playing", "paused", "stopped", "unknown"] = "unknown"
    player: str = Field(default="", max_length=200)
    album: str = Field(default="", max_length=300)
    position_seconds: Speed | None = None
    duration_seconds: Speed | None = None
    updated_at: AwareDatetime | None = None
    track_id: Digest | None = None
    artwork_version: Digest | None = None


class Heartbeat(BaseModel):
    name: Label
    os: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    timestamp: AwareDatetime
    battery: Battery | None = None
    cpu: CPU | None = None
    memory: Memory | None = None
    disks: list[Disk] = Field(default_factory=list, max_length=48)
    network: Network | None = None
    network_info: NetworkInfo | None = Field(default=None, exclude=True)
    top_processes: list[Process] = Field(default_factory=list, max_length=64)
    volume: Volume | None = None
    media: Media | None = None
    caffeinate: StrictBool | None = None
    capabilities: list[Label] = Field(default_factory=list, max_length=16)


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetVolume(EmptyPayload):
    percent: int = Field(ge=0, le=100, strict=True)


class SetMute(EmptyPayload):
    muted: StrictBool


class SetAudioDevice(EmptyPayload):
    device_id: DeviceUID


class AudioDeviceCommand(EmptyPayload):
    action: Literal["audio_device_set"]
    payload: SetAudioDevice


class VolumeCommand(EmptyPayload):
    action: Literal["volume_set"]
    payload: SetVolume


class MuteCommand(EmptyPayload):
    action: Literal["volume_mute"]
    payload: SetMute


class ButtonCommand(EmptyPayload):
    action: Literal["media_play_pause", "media_next", "media_previous", "lock", "display_sleep", "display_lock",
                    "sleep", "caffeinate_toggle", "clipboard_push"]
    payload: EmptyPayload = Field(default_factory=EmptyPayload)


class ConfirmedShutdownCommand(EmptyPayload):
    # Internal only. The device command endpoint must not accept this type.
    action: Literal["shutdown"] = "shutdown"
    payload: EmptyPayload = Field(default_factory=EmptyPayload)


Command = Annotated[VolumeCommand | MuteCommand | AudioDeviceCommand | ButtonCommand, Field(discriminator="action")]


class CommandResult(EmptyPayload):
    ok: StrictBool
    message: str = Field(default="", max_length=500)


class AliasChange(EmptyPayload):
    device_id: str = Field(pattern=DEVICE_PATTERN)
    alias: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None


@dataclass
class AgentCommand:
    action: str
    payload: dict
    queued_at: float
    sent_at: float | None = None
    result: CommandResult | None = None
    completed_at: datetime | None = None

    def status(self, now):
        if self.result is not None:
            return "succeeded" if self.result.ok else "failed"
        if self.sent_at is None:
            return "queued" if now - self.queued_at <= OFFLINE_AFTER else "expired"
        return "delivered" if now - self.sent_at <= RESULT_TIMEOUT else "expired"


@dataclass
class Device:
    snapshot: dict
    last_seen: datetime
    heartbeat_at: float
    commands: dict[UUID, AgentCommand] = field(default_factory=dict)
    artwork: tuple[str, bytes] | None = None
    stream_wake: asyncio.Event | None = None
    persisted_at: float | None = None

    def online(self, now):
        return now - self.heartbeat_at <= OFFLINE_AFTER

    def claim(self, now, minimum_age=0):
        # 由注册表持锁调用，SSE 和心跳共用同一份下发状态。
        if self.online(now):
            for identifier, command in self.commands.items():
                if command.status(now) == "queued" and now - command.queued_at >= minimum_age:
                    command.sent_at = now
                    yield {"id": str(identifier), "action": command.action, "payload": command.payload}


class AgentRegistry:
    def __init__(self, connect=None):
        self.devices: dict[str, Device] = {}
        self._aliases: dict[str, str] = {}
        self.connect = connect
        self.profiles_ready = False
        self.lock = Lock()

    def restore_profiles(self):
        with self.connect() as db:
            rows = db.execute("SELECT device_id, alias, snapshot, last_seen FROM agent_profiles ORDER BY last_seen DESC").fetchall()
        with self.lock:
            self._aliases = {row["device_id"]: row["alias"] for row in rows if row["alias"]}
            for row in rows:
                device_id = row["device_id"]
                if device_id == HUB_DEVICE_ID or not row["snapshot"] or not re.fullmatch(DEVICE_PATTERN, device_id):
                    continue
                try:
                    snapshot = Heartbeat.model_validate_json(row["snapshot"]).model_dump(mode="json")
                    seen = datetime.fromisoformat(row["last_seen"])
                    if seen.utcoffset() is None:
                        continue
                except (ValidationError, ValueError, TypeError):
                    continue
                self.devices[device_id] = Device(snapshot, seen, float("-inf"))
                if len(self.devices) >= MAX_DEVICES:
                    break
            self.profiles_ready = True

    @staticmethod
    def save_profile(db, device_id, device):
        db.execute("""INSERT INTO agent_profiles(device_id,snapshot,last_seen) VALUES(?,?,?)
                      ON CONFLICT(device_id) DO UPDATE SET snapshot=excluded.snapshot,last_seen=excluded.last_seen""",
                   (device_id, json.dumps(device.snapshot, ensure_ascii=False), device.last_seen.isoformat()))

    def flush_profiles(self):
        with self.lock, self.connect() as db:
            for device_id, device in self.devices.items():
                if device.persisted_at is not None:
                    self.save_profile(db, device_id, device)

    def aliases(self):
        with self.lock:
            return dict(self._aliases)

    def rename(self, payload):
        with self.lock:
            with self.connect() as db:
                if payload.alias is None:
                    db.execute("UPDATE agent_profiles SET alias=NULL WHERE device_id=?", (payload.device_id,))
                    db.execute("DELETE FROM agent_profiles WHERE device_id=? AND snapshot IS NULL", (payload.device_id,))
                else:
                    db.execute("""INSERT INTO agent_profiles(device_id,alias) VALUES(?,?)
                                  ON CONFLICT(device_id) DO UPDATE SET alias=excluded.alias""",
                               (payload.device_id, payload.alias))
            if payload.alias is None:
                self._aliases.pop(payload.device_id, None)
            else:
                self._aliases[payload.device_id] = payload.alias
            return dict(self._aliases)

    def remove(self, device_id):
        if device_id == HUB_DEVICE_ID:
            raise HTTPException(409, "不能移除本机 Hub")
        with self.lock:
            device = self.devices.get(device_id)
            if device is None:
                raise HTTPException(404, "找不到该设备")
            if device.online(monotonic()):
                raise HTTPException(409, "只能移除离线设备")
            with self.connect() as db:
                db.execute("DELETE FROM agent_profiles WHERE device_id=?", (device_id,))
            self._aliases.pop(device_id, None)
            del self.devices[device_id]
            wake, device.stream_wake = device.stream_wake, None
            return wake

    def heartbeat(self, device_id, snapshot):
        if device_id == HUB_DEVICE_ID:
            raise HTTPException(422, "设备 ID hub 保留给本机")
        network_info = snapshot.network_info
        snapshot = snapshot.model_dump(mode="json")
        if network_info and network_info.interfaces:
            network = snapshot["network"]
            if network is None:
                network = snapshot["network"] = Network().model_dump(mode="json")
            if not network["interfaces"]:
                network["interfaces"] = network_info.model_dump(mode="json")["interfaces"]
        media = snapshot["media"]
        if media and not media["track_id"]:
            media["track_id"] = metadata_track_id(media)
            # 旧版快照不能提供与曲目绑定的封面版本。
            media["artwork_version"] = None
        snapshot["top_processes"] = sorted(snapshot["top_processes"], key=lambda row: -row["rss_bytes"])[:5]
        with self.lock:
            now = monotonic()
            seen = datetime.now(timezone.utc)
            device = self.devices.get(device_id)
            if device is None:
                if len(self.devices) >= MAX_DEVICES:
                    raise HTTPException(409, "Agent 设备数量已达上限")
                device = self.devices[device_id] = Device(snapshot, seen, now)
            device.snapshot, device.last_seen, device.heartbeat_at = snapshot, seen, now
            if self.profiles_ready and (device.persisted_at is None or now - device.persisted_at >= PROFILE_INTERVAL):
                device.persisted_at = now
                try:
                    with self.connect() as db:
                        self.save_profile(db, device_id, device)
                except sqlite3.Error:
                    logger.warning("无法持久化 Agent 快照")
            minimum_age = STREAM_FALLBACK_DELAY if device.stream_wake is not None else 0
            commands = list(device.claim(now, minimum_age))
            media = snapshot["media"] or {}
            version = media.get("artwork_version") if media.get("track_id") else None
            if device.artwork and device.artwork[0] != version:
                device.artwork = None
            response = {"commands": commands}
            if version is not None:
                response["artwork_needed"] = device.artwork is None
            return response

    @staticmethod
    def public_snapshot(device):
        snapshot = device.snapshot
        media = snapshot["media"]
        if media and media["artwork_version"] and not device.artwork:
            return {**snapshot, "media": {**media, "artwork_version": None}}
        return snapshot

    def current_media(self, device_id):
        with self.lock:
            device = self.devices.get(device_id)
            return device.snapshot["media"] if device and device.online(monotonic()) else None

    def set_artwork(self, device_id, version, contents):
        with self.lock:
            device = self.devices.get(device_id)
            media = device.snapshot["media"] if device else None
            if not media or media["artwork_version"] != version or not media["track_id"]:
                raise HTTPException(409, "封面与当前曲目不匹配")
            if sha256(media["track_id"].encode() + contents).hexdigest() != version:
                raise HTTPException(422, "封面版本无效")
            device.artwork = version, contents

    def get_artwork(self, device_id, version=None):
        with self.lock:
            device = self.devices.get(device_id)
            if device and device.artwork and (version is None or device.artwork[0] == version):
                return device.artwork
        raise HTTPException(404, "该曲目暂无封面")

    def list(self):
        with self.lock:
            now = monotonic()
            return [
                {"device_id": device_id, "name": self._aliases.get(device_id, device.snapshot["name"]), "os": device.snapshot["os"],
                 "snapshot": self.public_snapshot(device), "last_seen": device.last_seen.isoformat(), "online": device.online(now),
                 "commands": [{"id": str(identifier), "action": command.action, "status": command.status(now),
                               **(command.result.model_dump() if command.result else {}),
                               **({"completed_at": command.completed_at.isoformat()} if command.completed_at else {})}
                              for identifier, command in device.commands.items()]}
                for device_id, device in self.devices.items()
            ]

    def enqueue(self, device_id, payload):
        return self.enqueue_batch(device_id, [payload])[0]

    def enqueue_batch(self, device_id, payloads):
        # 从异步 POST 路由调用，使 Event.set 在流所在的事件循环执行。
        with self.lock:
            now = monotonic()
            device = self.devices.get(device_id)
            if device is None:
                raise HTTPException(404, "找不到该设备")
            if not device.online(now):
                raise HTTPException(409, "设备已休眠或离线")
            for payload in payloads:
                if payload.action == "audio_device_set":
                    volume = device.snapshot["volume"] or {}
                    if not any(item["id"] == payload.payload.device_id for item in volume.get("devices", [])):
                        raise HTTPException(409, "该输出设备已不可用")
            extra = len(device.commands) + len(payloads) - MAX_COMMANDS
            if extra > 0:
                removable = [key for key, command in device.commands.items() if command.status(now) not in {"queued", "delivered"}]
                if len(removable) < extra:
                    raise HTTPException(429, "设备指令队列已满")
                for key in removable[:extra]:
                    del device.commands[key]
            queued = []
            for payload in payloads:
                identifier = uuid4()
                device.commands[identifier] = AgentCommand(payload.action, payload.payload.model_dump(), now)
                queued.append({"id": str(identifier), "status": "queued"})
            if device.stream_wake is not None:
                device.stream_wake.set()
            return queued

    def require_device(self, device_id):
        with self.lock:
            if device_id not in self.devices:
                raise HTTPException(404, "找不到该设备")

    async def stream(self, device_id):
        wake = asyncio.Event()
        with self.lock:
            device = self.devices.get(device_id)
            if device is None:
                return
            if device.stream_wake is not None:
                device.stream_wake.set()
            device.stream_wake = wake

        try:
            loop = asyncio.get_running_loop()
            keepalive_at = loop.time() + KEEPALIVE_SECONDS
            yield b": connected\n\n"
            while True:
                remaining = keepalive_at - loop.time()
                if remaining <= 0:
                    keepalive_at = loop.time() + KEEPALIVE_SECONDS
                    yield b": keepalive\n\n"
                    continue
                wake.clear()
                with self.lock:
                    if device.stream_wake is not wake:
                        return
                    command = next(device.claim(monotonic()), None)
                if command is not None:
                    yield f"event: command\ndata: {json.dumps(command, ensure_ascii=False)}\n\n".encode()
                    continue
                try:
                    await asyncio.wait_for(wake.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
        finally:
            with self.lock:
                if device.stream_wake is wake:
                    device.stream_wake = None

    def result(self, device_id, cmd_id, result):
        with self.lock:
            device = self.devices.get(device_id)
            command = device.commands.get(cmd_id) if device else None
            if command is None or command.sent_at is None:
                raise HTTPException(404, "找不到该设备的已下发指令")
            if command.result is not None and command.result != result:
                raise HTTPException(409, "已记录该指令的结果")
            if command.result is None:
                command.result = result
                command.completed_at = datetime.now(timezone.utc)


class AgentStreamResponse(StreamingResponse):
    async def stream_response(self, send):
        # 发送协程被取消时也立刻释放本设备的流注册。
        async with aclosing(self.body_iterator):
            await super().stream_response(send)


def agents_router(registry, authenticate):
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    @router.get("")
    def list_agents(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"devices": registry.list()}

    @router.get("/aliases")
    def aliases(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"aliases": registry.aliases()}

    @router.patch("/aliases")
    def rename(payload: AliasChange):
        return {"aliases": registry.rename(payload)}

    @router.delete("/{device_id}", status_code=204)
    async def remove(device_id: DeviceId):
        wake = await run_in_threadpool(registry.remove, device_id)
        if wake is not None:
            wake.set()

    @router.post("/{device_id}/heartbeat", dependencies=[Depends(authenticate)])
    def heartbeat(device_id: DeviceId, payload: Heartbeat):
        return registry.heartbeat(device_id, payload)

    @router.post("/{device_id}/artwork", status_code=204, dependencies=[Depends(authenticate)])
    async def upload_artwork(device_id: DeviceId, request: Request, version: Annotated[str, Query(pattern=HASH_PATTERN)]):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "image/jpeg":
            raise HTTPException(415, "封面必须使用 image/jpeg")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_ARTWORK:
                raise HTTPException(413, "封面过大")
            data.extend(chunk)
        try:
            with Image.open(BytesIO(data), formats=["JPEG"]) as image:
                if max(image.size) > 512:
                    raise ValueError("oversize")
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, "封面必须是最长边不超过 512 像素的有效 JPEG") from None
        registry.set_artwork(device_id, version, bytes(data))

    @router.get("/{device_id}/artwork")
    def artwork(device_id: DeviceId, request: Request, version: Annotated[str | None, Query(pattern=HASH_PATTERN)] = None):
        digest, contents = registry.get_artwork(device_id, version)
        etag = f'"{digest}"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=86400, immutable" if version else "private, no-cache",
                   "X-Content-Type-Options": "nosniff"}
        if any(tag.strip() in {etag, "*"} for tag in request.headers.get("if-none-match", "").split(",")):
            return Response(status_code=304, headers=headers)
        return Response(contents, media_type="image/jpeg", headers=headers)

    @router.post("/{device_id}/command", status_code=202)
    async def command(device_id: DeviceId, payload: Command):
        return registry.enqueue(device_id, payload)

    @router.get("/{device_id}/commands/stream", dependencies=[Depends(authenticate)])
    async def command_stream(device_id: DeviceId):
        registry.require_device(device_id)
        return AgentStreamResponse(registry.stream(device_id), media_type="text/event-stream",
                                   headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})

    @router.post("/{device_id}/commands/{cmd_id}/result", status_code=204, dependencies=[Depends(authenticate)])
    def result(device_id: DeviceId, cmd_id: UUID, payload: CommandResult):
        registry.result(device_id, cmd_id, payload)

    return router
