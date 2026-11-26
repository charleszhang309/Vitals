"""Explicit AI tool registry. Only chat input can authorize side effects."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import re
from time import monotonic
from typing import Literal
import unicodedata
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .agents import ButtonCommand, ConfirmedShutdownCommand, MuteCommand, VolumeCommand
from .calendar_service import NewEvent, event_time
from .life import CountdownInput, TaskInput
from .media import MediaAction, VolumeChange
from .scratchpad import TextRequest


logger = logging.getLogger(__name__)
CONFIRM_SECONDS = 60
Device = Literal["windows", "macbook"]
PowerMode = Literal["silent", "balanced", "performance"]
HostActionName = Literal["lock", "display_off", "display_lock", "sleep", "shutdown"]
MODES = {"silent": "静音", "balanced": "平衡", "performance": "性能"}
ACTIONS = {"lock": "锁屏", "display_off": "关闭显示器", "display_lock": "息屏锁屏", "sleep": "休眠", "shutdown": "关机"}
MAC_ACTIONS = {"lock": "lock", "display_off": "display_sleep", "display_lock": "display_lock", "sleep": "sleep"}
MAC_MEDIA = {"toggle": "media_play_pause", "next": "media_next", "previous": "media_previous"}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DeviceArgs(Args):
    device: Device


class CalendarQuery(Args):
    days: int = Field(default=7, ge=1, le=14)


class CurrencyQuery(Args):
    base: str = Field(pattern=r"^[A-Z]{3}$")
    target: str = Field(pattern=r"^[A-Z]{3}$")
    amount: float = Field(default=1, ge=0, le=1e12, allow_inf_nan=False)


class ScratchpadQuery(Args):
    limit: int = Field(default=5, ge=1, le=10)


class TodoToggle(Args):
    todo_id: int = Field(ge=1, le=2**63 - 1)
    completed: bool


class CalendarEntry(NewEvent):
    model_config = ConfigDict(extra="forbid", strict=True)


class PowerArgs(Args):
    mode: PowerMode


class MediaArgs(DeviceArgs):
    action: Literal["toggle", "next", "previous"]


class VolumeArgs(DeviceArgs):
    percent: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.percent is None and self.muted is None:
            raise ValueError("请指定音量或静音状态")
        return self


class HostArgs(DeviceArgs):
    action: HostActionName
    minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def valid_action(self):
        if self.minutes is not None and self.action != "shutdown":
            raise ValueError("只有关机可以指定分钟数")
        return self


@dataclass(frozen=True)
class Skill:
    schema: type[BaseModel]
    description: str
    label: str
    writes: bool = False


SKILLS = {
    "get_device_status": Skill(DeviceArgs, "读取指定设备的硬件、音量和媒体状态", "已读取设备状态"),
    "get_health_comparison": Skill(Args, "读取健康今日摘要与 7 天对比", "已读取健康摘要"),
    "get_exchange_rate": Skill(CurrencyQuery, "查询 ISO 币种之间的汇率和换算金额", "已查询汇率"),
    "get_calendar_events": Skill(CalendarQuery, "列出可见日历与今天起未来指定天数（最多 14 天）的日程", "已读取日历"),
    "list_todos": Skill(Args, "列出今日待办及其 ID", "已读取待办"),
    "list_scratchpad": Skill(ScratchpadQuery, "按时间读取最近的中转站记录，只读取文字及文件名", "已读取中转站"),
    "create_todo": Skill(TaskInput, "新增一条待办，按正常流程同步提醒事项", "已添加到待办", True),
    "toggle_todo": Skill(TodoToggle, "设置指定待办的完成状态，必要时先调用 list_todos", "已更新待办", True),
    "create_calendar_event": Skill(CalendarEntry, "在已配置的可写来源创建日程。非全天使用含时区的 ISO 8601，全天结束日期为排除边界", "已添加到日历", True),
    "create_countdown": Skill(CountdownInput, "以 YYYY-MM-DD 为目标日期创建倒数日", "已创建倒数日", True),
    "save_to_scratchpad": Skill(TextRequest, "将用户指定的文字保存到中转站", "已保存到中转站", True),
    "set_power_mode": Skill(PowerArgs, "切换 Windows 电源模式", "已切换电源模式", True),
    "control_media": Skill(MediaArgs, "在指定设备播放／暂停、下一首或上一首", "媒体操作已受理", True),
    "set_volume": Skill(VolumeArgs, "设置指定设备的音量百分比和／或静音", "音量操作已受理", True),
    "request_host_action": Skill(HostArgs, "仅生成需用户点击确认的操作。关机默认延时 1 分钟", "等待确认", True),
}

WRITE_INTENTS = {
    "create_todo": r"(?:加|添加|新增|创建|新建|记|列).{0,32}(?:待办|提醒事项|todo|reminder)|(?:待办|提醒事项).{0,20}(?:添加|新增|创建|新建|写入)",
    "toggle_todo": r"(?:勾选|标记|完成|取消完成|取消勾选|设为).{0,40}(?:待办|任务|提醒事项|todo)|(?:待办|任务|提醒事项|todo).{0,40}(?:完成|勾选)",
    "create_calendar_event": r"(?:添加|新增|建|创建|安排|预约|加入|记).{0,40}(?:日程|日历|会议|预约|calendarevent)",
    "create_countdown": r"(?:添加|加|新增|创建|建|设置).{0,32}(?:倒数日|倒计时|countdown)",
    "save_to_scratchpad": r"(?:保存|存|写|发|放).{0,28}(?:到|进|入|至|在)(?:中转站|暂存箱|scratchpad)|(?:中转站|暂存箱).{0,20}(?:新增|保存|写入)",
    "set_power_mode": r"(?:切|调|设置|设为|改).{0,32}(?:静音模式|平衡|性能|电源模式|silent|balanced|performance)",
    "control_media": r"(?<!正在)(?<!当前)(?<!在)(?:继续播放|播放(?!什么|的|器|列表)|暂停|下一首|上一首|下一曲|上一曲|skip|pause|play)",
    "set_volume": r"(?:音量|volume).{0,16}(?:调|设|改|降|升|到|为|\d)|(?:调|设|改|降|升).{0,24}(?:音量|volume)|(?:静音|mute)",
    "request_host_action": r"(?:锁屏|锁定|熄屏|[息关]屏|关闭显示器|休眠|睡眠|关机|shutdown|sleep|lock|displayoff)",
}
NON_ACTION = re.compile(r"不要|不用|无需|别(?:执行|关|锁|调|写|添加)|禁止|假设|假如|举例|如何|怎么|是什么意思|能否解释|"
                        r"是否(?:已|在)|了没有|按(?:照)?(?:里面|其中|文中|备注).{0,12}(?:指令|要求)|\bdon['’]?t\b", re.I)
QUOTES = re.compile(r"```[\s\S]*?```|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"|`[^`]*`")


def direct_intent(prompt):
    return "".join(QUOTES.sub("", unicodedata.normalize("NFKC", prompt)).casefold().split())


def allowed_writes(prompt):
    intent = direct_intent(prompt)
    return set() if NON_ACTION.search(intent) else {name for name, pattern in WRITE_INTENTS.items() if re.search(pattern, intent)}


def may_execute(name, args, prompt):
    intent = direct_intent(prompt)
    if hasattr(args, "device"):
        names_mac = bool(re.search(r"macbook|macos|\bmac\b|苹果电脑|笔记本", intent))
        names_windows = bool(re.search(r"windows|\bwin\b|台式机|台式电脑", intent))
        if args.device == "macbook" and not names_mac or args.device == "windows" and names_mac and not names_windows:
            return False
    if name == "set_volume":
        values = re.findall(r"(?<!\d)\d{1,3}(?=%|$|[。！!?，,吧])", intent)
        if args.percent is not None and values and args.percent not in map(int, values):
            return False
        if args.muted is not None:
            if not re.search(r"静音|mute", intent):
                return False
            unmute = bool(re.search(r"取消静音|解除静音|关闭静音|unmute", intent))
            if args.muted == unmute:
                return False
    if name == "request_host_action":
        pattern = {
            "lock": r"锁屏|锁定|lock", "display_off": r"熄屏|[息关]屏|关闭显示器|displayoff",
            "display_lock": r"(?:息屏|熄屏|关屏|关闭显示器).{0,8}锁屏|锁屏.{0,8}(?:息屏|熄屏|关屏|关闭显示器)",
            "sleep": r"休眠|睡眠|sleep", "shutdown": r"关机|shutdown",
        }[args.action]
        if not re.search(pattern, intent):
            return False
        if args.action == "shutdown":
            minutes = re.search(r"(\d{1,4})分钟", intent)
            return (args.minutes or 1) == (int(minutes[1]) if minutes else 1)
    return True


@dataclass(frozen=True)
class PendingAction:
    args: HostArgs
    deadline: float


class AiTools:
    def __init__(self, life, health, calendar, rates, store, control, media, agents):
        self.life, self.health, self.calendar, self.rates = life, health, calendar, rates
        self.store, self.control, self.media, self.agents = store, control, media, agents
        self.pending: dict[UUID, PendingAction] = {}
        self.mac_shutdown_task: asyncio.Task | None = None

    def definitions(self, allowed):
        return [{"type": "function", "function": {"name": name, "description": skill.description,
                 "parameters": skill.schema.model_json_schema()}}
                for name, skill in SKILLS.items() if not skill.writes or name in allowed]

    async def invoke(self, request, name, raw, prompt, allowed, used):
        skill = SKILLS.get(name)
        if skill is None or skill.writes and (name not in allowed or name in used):
            return {"ok": False, "error": "该工具未获当前用户消息授权，或本次操作已提交"}, None
        private_reads = {
            "list_scratchpad": r"中转站|暂存箱|scratchpad",
            "get_health_comparison": r"健康|步数|心率|睡眠|运动|状态|health",
            "get_calendar_events": r"日历|日程|行程|安排|会议|预约|calendar",
            "list_todos": r"待办|提醒事项|任务|todo",
        }
        if name in private_reads and not re.search(private_reads[name], direct_intent(prompt)):
            return {"ok": False, "error": "请在聊天中明确请求读取这类资料"}, None
        try:
            args = skill.schema.model_validate_json(raw)
            if skill.writes and not may_execute(name, args, prompt):
                return {"ok": False, "error": "操作与用户明确指定的设备、动作或数值不一致"}, None
        except ValidationError:
            return {"ok": False, "error": "工具参数无效或该设备不支持此操作"}, None
        if skill.writes:
            used.add(name)
        try:
            handler = getattr(self, name)
            result = await handler(request, args)
            if name == "request_host_action":
                return {"ok": True, "status": "pending_confirmation", "message": "等待用户点击确认"}, result
            return {"ok": True, **result}, None
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, str) else "操作暂不可用"
            return {"ok": False, "error": detail[:180]}, None
        except Exception as error:
            logger.info("AI 工具 %s 执行失败（%s）", name, type(error).__name__)
            return {"ok": False, "error": "操作暂未完成，请稍后重试"}, None

    def mac(self):
        devices = [item for item in self.agents.list() if item["os"] == "macos"]
        return next((item for item in devices if item["device_id"] == "macbook"),
                    devices[0] if len(devices) == 1 else None)

    def mac_id(self):
        device = self.mac()
        if not device or not device["online"]:
            raise HTTPException(409, "MacBook 未连接")
        return device["device_id"]

    def mac_shutdown_id(self):
        device = self.mac()
        if not device or not device["online"]:
            raise HTTPException(409, "MacBook 未连接")
        if "shutdown" not in device["snapshot"]["capabilities"]:
            raise HTTPException(409, "当前 Mac Agent 尚未支持关机，请先更新")
        return device["device_id"]

    async def shutdown_mac_after(self, device_id, minutes):
        await asyncio.sleep(minutes * 60)
        try:
            if self.mac_shutdown_id() != device_id:
                raise HTTPException(409, "MacBook 已切换")
            self.agents.enqueue(device_id, ConfirmedShutdownCommand())
        except HTTPException as error:
            logger.info("未下发 MacBook 定时关机（HTTP %s）", error.status_code)

    async def close(self):
        if self.mac_shutdown_task is not None:
            self.mac_shutdown_task.cancel()
            await asyncio.gather(self.mac_shutdown_task, return_exceptions=True)

    @staticmethod
    def public_media(media):
        return {key: media[key] for key in ("title", "artist", "player", "status") if key in media} if media else None

    @staticmethod
    def public_volume(volume):
        return {key: volume[key] for key in ("percent", "muted") if key in volume} if volume else None

    async def get_device_status(self, request, args):
        if args.device == "macbook":
            device = self.mac()
            if not device:
                return {"online": False}
            sample = device["snapshot"]
            return {"online": device["online"], "observed_at": device["last_seen"], "cpu": sample["cpu"],
                    "memory": sample["memory"], "battery": sample["battery"], "volume": self.public_volume(sample["volume"]),
                    "media": self.public_media(sample["media"])}
        monitor = getattr(request.app.state, "monitor", None)
        sample = getattr(monitor, "latest", None) or {}
        client = getattr(request.app.state, "media_client", None)
        volume, playback = await asyncio.gather(self.media.volume(client), self.media.state(client))
        track = next((row for row in playback["sessions"] if row["session_id"] == playback["current_session_id"]), None)
        return {"observed_at": sample.get("timestamp"), "cpu": sample.get("cpu"), "memory": sample.get("memory"),
                "gpus": [{key: item.get(key) for key in ("name", "usage_percent", "temperature_c")} for item in sample.get("gpus", [])[:4]],
                "power_mode": (self.control.latest or {}).get("mode"), "volume": self.public_volume(volume), "media": self.public_media(track)}

    async def get_health_comparison(self, request, args):
        summary, comparison = await asyncio.gather(run_in_threadpool(self.health.summary),
                                                   run_in_threadpool(self.health.weekly_comparison))
        return {"today": {key: summary.get(key) for key in ("date", "available", "totals", "latest", "sleep")},
                "comparison": comparison}

    async def get_exchange_rate(self, request, args):
        self.rates.validate(args.base, args.target)
        values = await self.rates.quotes(request.app.state.external_client, args.base, [args.target])
        quote = values[args.target]
        if quote["rate"] is None:
            raise HTTPException(503, "汇率暂不可用")
        return {"base": args.base, "target": args.target, "amount": args.amount,
                "converted": round(args.amount * quote["rate"], 4), **quote}

    async def get_calendar_events(self, request, args):
        result = await self.calendar.summary(request.app.state.external_client)
        cutoff = date.fromisoformat(result["date"]) + timedelta(days=args.days)
        events = []
        for item in result["events"]:
            start = event_time(item["start"], item["all_day"])
            day = start.astimezone(self.calendar.zone).date() if isinstance(start, datetime) else start
            if day < cutoff:
                events.append({**item, "notes": (item.get("notes") or "")[:280]})
        return {**result, "events": events[:25], "truncated": len(events) > 25}

    async def list_todos(self, request, args):
        data = await run_in_threadpool(self.life.tasks, "todos")
        return {**data, "items": data["items"][:50]}

    async def list_scratchpad(self, request, args):
        def recent():
            with self.store.connect() as db:
                rows = db.execute("""SELECT kind, substr(text, 1, 1000) AS text, filename, created_at
                    FROM scratchpad ORDER BY created_at DESC, id DESC LIMIT ?""", (args.limit,))
                return [dict(row) for row in rows]
        return {"records": await run_in_threadpool(recent)}

    async def create_todo(self, request, args):
        return {"item": await run_in_threadpool(self.life.add_task, "todos", args.title)}

    async def toggle_todo(self, request, args):
        await run_in_threadpool(self.life.change_task, "todos", args.todo_id, done=args.completed)
        return {"todo_id": args.todo_id, "completed": args.completed}

    async def create_calendar_event(self, request, args):
        event = await self.calendar.write(request.app.state.external_client, args.source_id, args)
        return {"event": event}

    async def create_countdown(self, request, args):
        return await run_in_threadpool(self.life.change_countdown, payload=args)

    async def save_to_scratchpad(self, request, args):
        if not args.text.strip():
            raise HTTPException(422, "文字不能为空")
        item = await run_in_threadpool(self.store.add_text, args.text)
        return {"id": item["id"]}

    async def set_power_mode(self, request, args):
        result = await run_in_threadpool(self.control.set_power, args.mode)
        return {"mode": result["mode"], "label": f"已切换到{MODES[args.mode]}模式",
                "fan_control": result["fan_control"].get("status")}

    async def control_media(self, request, args):
        if args.device == "macbook":
            self.agents.enqueue(self.mac_id(), ButtonCommand(action=MAC_MEDIA[args.action]))
            return {"status": "queued", "label": "已发送 MacBook 媒体指令"}
        client = getattr(request.app.state, "media_client", None)
        state = await self.media.state(client)
        track = next((row for row in state["sessions"] if row["session_id"] == state["current_session_id"]), None)
        if not track:
            raise HTTPException(409, "Windows 当前没有播放器会话")
        action = args.action
        if action == "toggle" and not track.get("actions", {}).get("toggle"):
            action = "pause" if track.get("status") == "playing" else "play"
        await self.media.action(client, MediaAction(session_id=track["session_id"], action=action, track_id=track.get("track_id")))
        return {"status": "accepted", "label": "Windows 已接受媒体操作"}

    async def set_volume(self, request, args):
        if args.device == "macbook":
            device_id = self.mac_id()
            commands = []
            if args.percent is not None:
                commands.append(VolumeCommand(action="volume_set", payload={"percent": args.percent}))
            if args.muted is not None:
                commands.append(MuteCommand(action="volume_mute", payload={"muted": args.muted}))
            self.agents.enqueue_batch(device_id, commands)
            return {"status": "queued", "label": "已发送 MacBook 音量指令"}
        client = getattr(request.app.state, "media_client", None)
        volume = await self.media.set_volume(client, VolumeChange(percent=args.percent, muted=args.muted))
        return {"status": "accepted", "label": "Windows 音量已更新", "volume": self.public_volume(volume)}

    async def request_host_action(self, request, args):
        if args.device == "macbook" and args.action == "shutdown":
            self.mac_shutdown_id()
        return self.stage_action(args)

    def stage_action(self, args):
        now = monotonic()
        self.pending = {key: item for key, item in self.pending.items() if item.deadline > now}
        if len(self.pending) >= 32:
            raise HTTPException(429, "待确认动作过多，请稍后再试")
        identifier = uuid4()
        self.pending[identifier] = PendingAction(args, now + CONFIRM_SECONDS)
        target = "Windows" if args.device == "windows" else "MacBook"
        action = f"{args.minutes or 1} 分钟后关机" if args.action == "shutdown" else ACTIONS[args.action]
        return {"id": str(identifier), "label": f"{target} · {action}",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=CONFIRM_SECONDS)).isoformat()}

    def consume(self, identifier):
        # Runs on the API event loop, in one synchronous step before any side effect.
        pending = self.pending.pop(identifier, None)
        if pending is None:
            raise HTTPException(404, "此操作已处理或不存在")
        if monotonic() >= pending.deadline:
            raise HTTPException(410, "此操作已过期，请重新发起")
        return pending.args

    async def confirm(self, identifier):
        args = self.consume(identifier)
        if args.device == "macbook":
            if args.action == "shutdown":
                device_id = self.mac_shutdown_id()
                if self.mac_shutdown_task is not None and not self.mac_shutdown_task.done():
                    raise HTTPException(409, "MacBook 已有定时关机")
                minutes = args.minutes or 1
                self.mac_shutdown_task = asyncio.create_task(self.shutdown_mac_after(device_id, minutes))
                return {"status": "scheduled", "message": f"已安排 MacBook {minutes} 分钟后关机"}
            self.agents.enqueue(self.mac_id(), ButtonCommand(action=MAC_ACTIONS[args.action]))
            return {"status": "queued", "message": "已发送至 MacBook，等待设备执行"}
        if args.action == "shutdown":
            return {"status": "executed", "message": "已安排 Windows 关机",
                    "scheduled_at": self.control.schedule_shutdown(args.minutes or 1)}
        await run_in_threadpool(self.control.confirmed_host_action, args.action)
        return {"status": "executed", "message": "Windows 已接受操作"}
