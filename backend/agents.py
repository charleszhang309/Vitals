"""远程设备快照、一次性指令下发及共享 Agent 鉴权。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hmac import compare_digest
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StringConstraints


OFFLINE_AFTER = 15
RESULT_TIMEOUT = 60
MAX_DEVICES = 32
MAX_COMMANDS = 32
DeviceId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
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
    minutes_left: Annotated[int, Field(ge=0, strict=True)] | None = None


class CPU(BaseModel):
    usage_percent: Percent


class Memory(BaseModel):
    used_bytes: Bytes
    total_bytes: Bytes
    percent: Percent


class Disk(BaseModel):
    mountpoint: Label
    free_bytes: Bytes
    total_bytes: Bytes


class Network(BaseModel):
    upload_bytes_per_sec: Speed | None = None
    download_bytes_per_sec: Speed | None = None


class Process(BaseModel):
    name: Label
    rss_bytes: Bytes
    pid: Annotated[int, Field(ge=0, strict=True)] | None = None


class Volume(BaseModel):
    percent: Percent
    muted: StrictBool


class Media(BaseModel):
    title: str = Field(default="", max_length=300)
    artist: str = Field(default="", max_length=300)
    status: Literal["playing", "paused", "stopped", "unknown"] = "unknown"
    player: str = Field(default="", max_length=200)


class Heartbeat(BaseModel):
    name: Label
    os: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    timestamp: AwareDatetime
    battery: Battery | None = None
    cpu: CPU | None = None
    memory: Memory | None = None
    disks: list[Disk] = Field(default_factory=list, max_length=48)
    network: Network | None = None
    top_processes: list[Process] = Field(default_factory=list, max_length=64)
    volume: Volume | None = None
    media: Media | None = None


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetVolume(EmptyPayload):
    percent: int = Field(ge=0, le=100, strict=True)


class SetMute(EmptyPayload):
    muted: StrictBool


class VolumeCommand(EmptyPayload):
    action: Literal["volume_set"]
    payload: SetVolume


class MuteCommand(EmptyPayload):
    action: Literal["volume_mute"]
    payload: SetMute


class ButtonCommand(EmptyPayload):
    action: Literal["media_play_pause", "media_next", "media_previous", "lock", "display_sleep", "sleep"]
    payload: EmptyPayload = Field(default_factory=EmptyPayload)


Command = Annotated[VolumeCommand | MuteCommand | ButtonCommand, Field(discriminator="action")]


class CommandResult(EmptyPayload):
    ok: StrictBool
    message: str = Field(default="", max_length=500)


@dataclass
class AgentCommand:
    action: str
    payload: dict
    queued_at: float
    sent_at: float | None = None
    result: CommandResult | None = None

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

    def online(self, now):
        return now - self.heartbeat_at <= OFFLINE_AFTER


class AgentRegistry:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.lock = Lock()

    def heartbeat(self, device_id, snapshot):
        snapshot = snapshot.model_dump(mode="json")
        snapshot["top_processes"] = sorted(snapshot["top_processes"], key=lambda row: -row["rss_bytes"])[:5]
        with self.lock:
            now = monotonic()
            seen = datetime.now(timezone.utc)
            device = self.devices.get(device_id)
            if device is None:
                if len(self.devices) >= MAX_DEVICES:
                    raise HTTPException(409, "Agent 设备数量已达上限")
                self.devices[device_id] = Device(snapshot, seen, now)
                return []
            device.snapshot, device.last_seen, device.heartbeat_at = snapshot, seen, now
            commands = []
            for identifier, command in device.commands.items():
                if command.status(now) == "queued":
                    command.sent_at = now
                    commands.append({"id": str(identifier), "action": command.action, "payload": command.payload})
            return commands

    def list(self):
        with self.lock:
            now = monotonic()
            return [
                {"device_id": device_id, "name": device.snapshot["name"], "os": device.snapshot["os"],
                 "snapshot": device.snapshot, "last_seen": device.last_seen.isoformat(), "online": device.online(now),
                 "commands": [{"id": str(identifier), "action": command.action, "status": command.status(now),
                               **(command.result.model_dump() if command.result else {})}
                              for identifier, command in device.commands.items()]}
                for device_id, device in self.devices.items()
            ]

    def enqueue(self, device_id, payload):
        with self.lock:
            now = monotonic()
            device = self.devices.get(device_id)
            if device is None:
                raise HTTPException(404, "找不到该设备")
            if not device.online(now):
                raise HTTPException(409, "设备已休眠或离线")
            if len(device.commands) >= MAX_COMMANDS:
                oldest = next((key for key, command in device.commands.items()
                               if command.status(now) not in {"queued", "delivered"}), None)
                if oldest is None:
                    raise HTTPException(429, "设备指令队列已满")
                del device.commands[oldest]
            identifier = uuid4()
            device.commands[identifier] = AgentCommand(payload.action, payload.payload.model_dump(), now)
            return {"id": str(identifier), "status": "queued"}

    def result(self, device_id, cmd_id, result):
        with self.lock:
            device = self.devices.get(device_id)
            command = device.commands.get(cmd_id) if device else None
            if command is None or command.sent_at is None:
                raise HTTPException(404, "找不到该设备的已下发指令")
            if command.result is not None and command.result != result:
                raise HTTPException(409, "已记录该指令的结果")
            command.result = result


def agents_router(registry, authenticate):
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    @router.get("")
    def list_agents(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"devices": registry.list()}

    @router.post("/{device_id}/heartbeat", dependencies=[Depends(authenticate)])
    def heartbeat(device_id: DeviceId, payload: Heartbeat):
        return {"commands": registry.heartbeat(device_id, payload)}

    @router.post("/{device_id}/command", status_code=202)
    def command(device_id: DeviceId, payload: Command):
        return registry.enqueue(device_id, payload)

    @router.post("/{device_id}/commands/{cmd_id}/result", status_code=204, dependencies=[Depends(authenticate)])
    def result(device_id: DeviceId, cmd_id: UUID, payload: CommandResult):
        registry.result(device_id, cmd_id, payload)

    return router
