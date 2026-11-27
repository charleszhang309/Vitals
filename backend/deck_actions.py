"""Typed built-in Deck operations and ordered macro execution."""

import asyncio
import json
from uuid import UUID
from typing import Literal

from fastapi import HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mac_agent.desktop_input import PRESETS, dispatch_input, parse_hotkey, text_units

from .agents import (AudioDeviceCommand, ButtonCommand, DeckKeyboardCommand, MuteCommand,
                     VolumeCommand)
from .config import load_settings
from .deck_plugins import DeckPluginError
from .launchers import launch_app
from .media import MediaAction, VolumeChange


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Hotkey(Arguments):
    shortcut: str = Field(min_length=1, max_length=48)


class Preset(Arguments):
    preset: Literal[tuple(PRESETS)]


class Text(Arguments):
    text: str = Field(min_length=1, max_length=500)


class Id(Arguments):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class Webhook(Id):
    body: str = Field(default="", max_length=2048)


class Direction(Arguments):
    direction: Literal["output", "input"] = "output"


class StepVolume(Direction):
    step: int = Field(ge=-100, le=100)


class AudioDevice(Direction):
    device_id: str = Field(min_length=1, max_length=256)


class Media(Arguments):
    action: Literal["toggle", "next", "previous"]


class Power(Arguments):
    mode: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class Desktop(Arguments):
    action: Literal["lock", "display_off"]


class Focus(Arguments):
    action: Literal["start", "pause", "toggle", "reset"]
    minutes: int = Field(default=25, ge=1, le=180)


class PluginAction(Arguments):
    plugin_id: str = Field(min_length=1, max_length=48)
    action_id: str = Field(min_length=1, max_length=48)
    args: dict = Field(default_factory=dict, max_length=24)


def field(identifier, name, kind="string", **options):
    return {"id": identifier, "name": name, "type": kind, **options}


def choice(identifier, name, choices, default=None):
    return field(identifier, name, "select", options=[
        {"value": value, "label": label} for value, label in choices
    ], default=default or choices[0][0])


direction = choice("direction", "音频方向", [("output", "输出"), ("input", "输入")])
DESKTOP_PRESET_NAMES = ["左侧桌面", "右侧桌面", "窗口左分屏", "窗口右分屏", "新建虚拟桌面",
                        "关闭虚拟桌面", "任务视图", "调度中心", "切换应用"]
BUILTINS = {
    "hotkey": ("快捷键", Hotkey, [field("shortcut", "组合键", default="ctrl+shift+a")]),
    "preset": ("桌面预设", Preset, [choice("preset", "预设", list(zip(PRESETS, DESKTOP_PRESET_NAMES)))]),
    "text": ("文本片段", Text, [field("text", "输入文本", "textarea")]),
    "webhook": ("HTTP Webhook", Webhook, [field("id", "Webhook", "webhook"), field("body", "JSON 请求体", "textarea")]),
    "mute": ("静音切换", Direction, [direction]),
    "volume": ("音量步进", StepVolume, [direction, field("step", "音量变化", "integer", default=10, minimum=-100, maximum=100)]),
    "audio_device": ("切换音频设备", AudioDevice, [direction, field("device_id", "设备", "audio_device")]),
    "media": ("媒体控制", Media, [choice("action", "动作", [("toggle", "播放 / 暂停"), ("next", "下一首"), ("previous", "上一首")])]),
    "power": ("电源模式", Power, [field("mode", "模式", "power", default="balanced")]),
    "launcher": ("快捷启动", Id, [field("id", "启动项", "launcher")]),
    "desktop": ("锁屏 / 息屏", Desktop, [choice("action", "动作", [("lock", "锁屏"), ("display_off", "关闭显示器")])]),
    "focus": ("专注计时", Focus, [choice("action", "动作", [("toggle", "开始 / 暂停"), ("start", "开始"), ("pause", "暂停"), ("reset", "重置")]),
                               field("minutes", "分钟", "integer", default=25, minimum=1, maximum=180)]),
    "screenshot": ("看一眼屏幕", Arguments, []),
}
def parameters(action_id, args):
    model = PluginAction if action_id == "plugin" else BUILTINS.get(action_id, (None, None, None))[1]
    if not model:
        raise ValueError("未知的 Deck 动作")
    try:
        spec = model.model_validate(args)
        if action_id == "hotkey":
            parse_hotkey(spec.shortcut)
        elif action_id == "text":
            text_units(spec.text)
        elif action_id == "webhook" and spec.body:
            body = json.loads(spec.body)
            json.dumps(body, allow_nan=False)
    except (ValidationError, ValueError, RecursionError):
        raise ValueError("Deck 动作参数无效") from None
    return spec


async def execute_macro(steps, invoke, sleep=asyncio.sleep):
    results = []
    for step in steps:
        if step.delay_ms:
            await sleep(step.delay_ms / 1000)
        results.append(await invoke(step.action))
    return results


class DeckActions:
    def __init__(self, settings, config_path, platform, agents, power, media, plugins):
        self.settings, self.config_path, self.platform = settings, config_path, platform
        self.agents, self.power, self.media, self.plugins = agents, power, media, plugins
        self.plugin_lock = asyncio.Lock()

    def target(self, device_id):
        if device_id == "hub":
            return self.platform, None
        device = next((item for item in self.agents.list() if item["device_id"] == device_id), None)
        if device is None or not device["online"]:
            raise HTTPException(409, "设备已休眠或离线")
        return device["os"], device["snapshot"]

    def settings_now(self):
        return load_settings(self.config_path) if self.config_path else self.settings

    async def catalog(self):
        settings = await asyncio.to_thread(self.settings_now)
        return {
            "actions": [{"id": id_, "name": name, "parameters": fields} for id_, (name, _, fields) in BUILTINS.items()],
            "plugins": self.plugins.catalog(),
            "webhooks": [{ "id": entry.id, "name": entry.name, "method": entry.method } for entry in settings.deck.webhooks],
            "launchers": [entry.model_dump(exclude={"target"}) for entry in settings.launchers],
            "power_modes": list(settings.power_modes),
        }

    async def audio(self, client, snapshot):
        return (snapshot.get("volume") or {}) if snapshot is not None else await self.media.volume(client)

    async def remote(self, target, command, *, wait=True):
        receipt = self.agents.enqueue(target, command)
        if not wait:
            return
        identifier = UUID(receipt["id"])
        try:
            for _ in range(60):
                status, result = self.agents.command_state(target, identifier)
                if result is not None:
                    if result.ok:
                        return
                    raise HTTPException(409, result.message or "设备执行 Deck 动作失败")
                if status == "expired":
                    break
                await asyncio.sleep(0.1)
            raise HTTPException(504, "未收到设备的执行确认")
        finally:
            self.agents.cancel_queued(target, identifier)

    async def plugin_call(self, method, *args):
        async with self.plugin_lock:
            # Keep the lock until the local plugin returns, including after cancellation.
            worker = asyncio.create_task(asyncio.to_thread(method, *args))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.gather(worker, return_exceptions=True)
                raise
            except DeckPluginError as error:
                messages = {"invalid_parameters": "Deck 插件参数与声明不符", "disabled": "Deck 插件未启用",
                            "unavailable": "Deck 插件功能暂不可用", "not_found": "找不到该 Deck 插件项目"}
                raise HTTPException(422 if error.code == "invalid_parameters" else 409,
                                    messages.get(error.code, "Deck 插件执行失败")) from None

    async def invoke(self, action, target, client):
        action_id, args = action.id, parameters(action.id, action.params)
        platform, remote = self.target(target)
        is_remote = remote is not None
        if action_id in {"hotkey", "preset", "text"}:
            if action_id == "preset" and platform not in PRESETS[args.preset]:
                raise HTTPException(409, "此平台不支持该桌面预设")
            payload = args.model_dump()
            if is_remote:
                await self.remote(target, DeckKeyboardCommand(payload=payload))
            else:
                await asyncio.to_thread(dispatch_input, platform, **payload)
        elif action_id == "webhook":
            settings = await asyncio.to_thread(self.settings_now)
            entry = next((item for item in settings.deck.webhooks if item.id == args.id), None)
            if not entry:
                raise HTTPException(404, "未配置该 Deck Webhook")
            kwargs = {"json": json.loads(args.body)} if entry.method == "POST" and args.body else {}
            try:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as outbound:
                    async with outbound.stream(entry.method, str(entry.url), follow_redirects=False, **kwargs) as response:
                        response.raise_for_status()
            except httpx.HTTPError:
                raise HTTPException(502, "Deck Webhook 请求失败") from None
        elif action_id in {"mute", "volume", "audio_device"}:
            prefix = "input_" if args.direction == "input" else ""
            volume = await self.audio(client, remote)
            if action_id == "mute":
                current = volume.get(prefix + "muted")
                if current is None:
                    raise HTTPException(409, "音频设备暂不可用")
                change = {"muted": not current}
                command = MuteCommand(action=prefix + "volume_mute", payload=change) if is_remote else None
            elif action_id == "volume":
                current = volume.get(prefix + "percent")
                if current is None:
                    raise HTTPException(409, "音频设备暂不可用")
                change = {"percent": round(min(100, max(0, current + args.step)))}
                command = VolumeCommand(action=prefix + "volume_set", payload=change) if is_remote else None
            else:
                if not any(item["id"] == args.device_id for item in volume.get(prefix + "devices", [])):
                    raise HTTPException(409, "音频设备暂不可用")
                change = {"device_id": args.device_id}
                command = AudioDeviceCommand(action=prefix + "audio_device_set", payload=change) if is_remote else None
            if is_remote:
                await self.remote(target, command)
            else:
                await self.media.set_volume(client, VolumeChange(**{prefix + key: value for key, value in change.items()}))
        elif action_id == "media":
            if is_remote:
                kind = {"toggle": "play_pause", "next": "next", "previous": "previous"}[args.action]
                await self.remote(target, ButtonCommand(action="media_" + kind))
            else:
                session = (await self.media.state(client)).get("current_session_id")
                if not session:
                    raise HTTPException(409, "没有活跃的媒体会话")
                await self.media.action(client, MediaAction(session_id=session, action=args.action))
        elif action_id == "power":
            if is_remote or platform != "windows":
                raise HTTPException(409, "电源模式仅支持 Windows 主机")
            await asyncio.to_thread(self.power.set_power, args.mode)
        elif action_id == "launcher":
            settings = await asyncio.to_thread(self.settings_now)
            entry = next((item for item in settings.launchers if item.id == args.id), None)
            if entry is None:
                raise HTTPException(404, "找不到该启动项")
            if entry.kind == "url":
                return {"effect": "link", "id": entry.id}
            if is_remote:
                raise HTTPException(409, "应用启动项仅在主中心运行")
            await asyncio.to_thread(launch_app, entry, platform)
        elif action_id == "desktop":
            if is_remote:
                await self.remote(target, ButtonCommand(action="lock" if args.action == "lock" else "display_sleep"), wait=False)
            else:
                await asyncio.to_thread(self.power.confirmed_host_action, args.action)
        elif action_id == "focus":
            return {"effect": "focus", **args.model_dump()}
        elif action_id == "screenshot":
            return {"effect": "screenshot"}
        elif action_id == "plugin":
            value = await self.plugin_call(self.plugins.execute_action, args.plugin_id, args.action_id, args.args)
            return {"value": value}
        return {}
