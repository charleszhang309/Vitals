"""Run the Vitals Mac agent in the logged-in user's session."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import signal
import socket
import subprocess
import sys
from threading import Event, Lock, Thread
from time import monotonic, time
from uuid import UUID

import httpx
import psutil
from PIL import Image

if __package__ in ("", None):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac_agent.api import CONFIG_PATH, Config
from mac_agent.clipboard import notify, push
from mac_agent.macos import Caffeinate, Sampler, execute_command
from mac_agent.playback import MEDIA_ACTIONS, read_playback
from mac_agent.quick_controls import QuickControls, ScriptError
from mac_agent.reminders import sync_once
from mac_agent.screenshot import ScreenRecordingDenied, capture_mac
from mac_agent.state import load_state, save_state, single_instance, state_directory

HEARTBEAT_SECONDS = 3
REMINDERS_SECONDS = 30
STREAM_BACKOFF = (1, 2, 5)
STREAM_KEEPALIVE_SECONDS = 20
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
RECEIPT_TTL = 48 * 3600
REFRESH_ACTIONS = {"volume_set", "volume_mute", "audio_device_set", "input_volume_set", "input_volume_mute",
                   "input_audio_device_set", "media_play_pause", "media_next", "media_previous", "caffeinate_toggle"}
FAILURES = (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError, KeyError, psutil.Error,
            subprocess.TimeoutExpired)
logger = logging.getLogger(__name__)


class SnapshotPublisher:
    def __init__(self, client, config, caffeinate, inbox):
        self.client = client
        self.path = f"/api/agents/{config.device_id}"
        self.device_name = config.device_name
        self.caffeinate = caffeinate
        self.inbox = inbox
        self.snapshot = {}
        self.lock = Lock()
        self.generation = 0
        self.changed = {}

    def revision(self):
        with self.lock:
            return self.generation

    def media_identity(self):
        with self.lock:
            media = self.snapshot.get("media") or {}
            return media.get("track_id"), media.get("status")

    def _post(self):
        response = self.client.post(self.path + "/heartbeat", json={
            "name": self.device_name, "os": "macos",
            "timestamp": datetime.now(timezone.utc).isoformat(), **self.snapshot,
            "capabilities": ["screenshot", "deck_keyboard"],
        })
        response.raise_for_status()
        reply = response.json()
        commands = reply["commands"]
        if not isinstance(commands, list):
            raise ValueError("心跳响应中的 commands 必须是数组")
        if commands:
            self.inbox.put_nowait(commands)
        return reply.get("artwork_needed") is True

    def hardware(self, snapshot, revision, artwork):
        # Samples are gathered outside this lock. Rebase any controls that were
        # already published while the full hardware sample was being collected.
        with self.lock:
            for field, generation in self.changed.items():
                if generation > revision and field in snapshot:
                    snapshot[field] = self.snapshot[field]
            if self.caffeinate is not None:
                snapshot["caffeinate"] = self.caffeinate.active()
            self.snapshot = snapshot
            needs_artwork = self._post()
            version = (snapshot.get("media") or {}).get("artwork_version")
        if needs_artwork and artwork is not None:
            contents = artwork.bytes_for(version)
            if contents:
                try:
                    response = self.client.post(self.path + "/artwork", params={"version": version},
                                                headers={"Content-Type": "image/jpeg"}, content=contents)
                    if response.status_code != 409:
                        response.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.warning("封面上传失败：%s", exc)

    def control(self, updates):
        with self.lock:
            if "volume" in updates:
                updates = {**updates, "volume": {**(self.snapshot.get("volume") or {}), **updates["volume"]}}
            if media := updates.get("media"):
                current = self.snapshot.get("media") or {}
                if media.get("track_id") == current.get("track_id"):
                    updates = {**updates, "media": {**media, "artwork_version": current.get("artwork_version")}}
            self.generation += 1
            self.changed.update(dict.fromkeys(updates, self.generation))
            self.snapshot.update(updates)
            self._post()


def command_events(lines):
    event = None
    data = []
    for line in lines:
        if not line:
            if event == "command" and data:
                try:
                    command = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    logger.debug("忽略格式错误的 SSE 指令")
                else:
                    if isinstance(command, dict):
                        yield command
            event, data = None, []
        elif not line.startswith(":"):
            field, separator, value = line.partition(":")
            if separator and field == "event":
                event = value.removeprefix(" ")
            elif separator and field == "data":
                data.append(value.removeprefix(" "))


def listen_commands(config, stopped, inbox):
    path = f"/api/agents/{config.device_id}/commands/stream"
    retry = 0
    with config.client(timeout=httpx.Timeout(5, read=26)) as client:
        while not stopped.is_set():
            connected_at = None
            try:
                with client.stream("GET", path, headers={"Accept": "text/event-stream"}) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise ValueError("指令流响应格式不正确")
                    connected_at = monotonic()
                    logger.info("SSE 指令流已连接")
                    for command in command_events(response.iter_lines()):
                        if stopped.is_set():
                            return
                        logger.debug("SSE 收到指令 %s", command.get("id"))
                        inbox.put_nowait([command])
                        retry = 0
            except (httpx.HTTPError, ValueError) as exc:
                # Older or restarting centers continue dispatching via heartbeat.
                logger.debug("指令流暂不可用（%s）", type(exc).__name__)
            if connected_at is not None and monotonic() - connected_at >= STREAM_KEEPALIVE_SECONDS:
                retry = 0
            stopped.wait(STREAM_BACKOFF[retry])
            retry = min(retry + 1, len(STREAM_BACKOFF) - 1)


def process_screenshot(client, device_id, command_id, payload):
    try:
        if payload != {} or str(UUID(command_id)) != command_id:
            return
    except ValueError:
        return

    route = f"/api/agents/{device_id}/commands/{command_id}/screenshot"
    try:
        response = client.post(route + "/start")
        if response.status_code != 204:
            return
    except httpx.HTTPError:
        return

    try:
        media_type, contents = capture_mac()
        response = client.post(route, content=contents, headers={"Content-Type": media_type})
        if response.status_code not in (404, 410):
            response.raise_for_status()
        return
    except ScreenRecordingDenied:
        code = "permission"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, Image.DecompressionBombError):
        code = "capture"
    try:
        client.post(route + "/error/" + code)
    except httpx.HTTPError:
        pass


def process_commands(client, config, commands, directory, caffeinate, quick, publisher, media_refresh):
    path = directory / "command-receipts.json"
    now = time()
    receipts = {key: value for key, value in load_state(path, {}).items() if now - value["time"] < RECEIPT_TTL}
    def send_clipboard():
        push(client)
        notify("push")

    def report(command_id, receipt):
        route = f"/api/agents/{config.device_id}/commands/{command_id}/result"
        try:
            response = client.post(route, json=receipt["result"])
            if response.status_code not in (404, 409):
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("指令 %s 回报失败，将重试：%s", command_id, exc)
        else:
            receipt["reported"] = True
            save_state(path, receipts)

    retried = [(key, value) for key, value in receipts.items() if not value["reported"]]
    for command in commands:
        if not isinstance(command, dict):
            continue
        command_id = command.get("id")
        if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            logger.warning("忽略没有有效 ID 的指令")
            continue
        if command.get("action") == "screenshot":
            process_screenshot(client, config.device_id, command_id, command.get("payload", {}))
            continue
        if command_id in receipts:
            continue
        receipt = {"time": time(), "reported": False,
                   "result": {"ok": False, "message": "执行时进程中断，结果未知"}}
        receipts[command_id] = receipt
        save_state(path, receipts)
        update = None
        try:
            action = command.get("action")
            if not isinstance(action, str):
                raise ValueError("action 必须是字符串")
            params = command.get("payload", {})
            started = monotonic()
            try:
                update = execute_command(action, params, caffeinate=caffeinate, clipboard_push=send_clipboard, quick=quick)
            except ScriptError as exc:
                if exc.number != -1743:
                    raise
                # A denied Apple event was never delivered. Keep support for
                # systems that granted Automation to osascript only.
                logger.debug("进程内 AppleScript 权限受限，使用 osascript")
                update = execute_command(action, params, caffeinate=caffeinate, clipboard_push=send_clipboard)
            logger.debug("指令 %s %s 本机执行耗时 %.1f ms", command_id, action, 1000 * (monotonic() - started))
            receipt["result"] = {"ok": True, "message": "已完成"}
        except FAILURES as exc:
            receipt["result"] = {"ok": False, "message": str(exc)[:500]}
        save_state(path, receipts)
        report(command_id, receipt)
        if receipt["result"]["ok"] and action in REFRESH_ACTIONS:
            try:
                if update is not None:
                    publisher.control(update)
                elif action in MEDIA_ACTIONS:
                    media_refresh.set()
            except FAILURES as exc:
                logger.warning("即时心跳失败，将在定时心跳重试：%s", exc)
    for command_id, receipt in retried:
        report(command_id, receipt)


def command_worker(client, config, directory, caffeinate, stopped, inbox, quick, publisher, media_refresh):
    while not stopped.is_set():
        try:
            commands = inbox.get(timeout=HEARTBEAT_SECONDS)
        except Empty:
            commands = []
        if stopped.is_set():
            break
        try:
            process_commands(client, config, commands, directory, caffeinate, quick, publisher, media_refresh)
        except FAILURES as exc:
            logger.warning("处理指令失败：%s", exc)


def hardware_heartbeats(publisher, sampler, listener, stopped):
    next_sample = monotonic()
    while not stopped.is_set():
        if next_sample <= monotonic():
            next_sample = monotonic() + HEARTBEAT_SECONDS
        try:
            revision = publisher.revision()
            snapshot = sampler.sample()
            publisher.hardware(snapshot, revision, sampler.playback)
            if listener.ident is None and not stopped.is_set():
                listener.start()
        except FAILURES as exc:
            logger.warning("心跳失败：%s", exc)
        stopped.wait(max(0, next_sample - monotonic()))


def refresh_media(publisher, requested, stopped):
    while not stopped.is_set():
        if not requested.wait(1) or stopped.is_set():
            continue
        requested.clear()
        previous = publisher.media_identity()
        deadline = monotonic() + 0.65
        try:
            while not stopped.is_set():
                media, _ = read_playback()
                identity = (media["track_id"], media["status"]) if media else (None, None)
                if identity != previous or monotonic() >= deadline:
                    publisher.control({"media": media})
                    break
        except FAILURES as exc:
            logger.warning("播放状态即时心跳失败：%s", exc)


def sync_reminders(config, directory, stopped):
    with config.client() as client:
        while not stopped.is_set():
            try:
                sync_once(client, config, directory)
            except FAILURES as exc:
                logger.warning("提醒事项失败：%s", exc)
            stopped.wait(REMINDERS_SECONDS)


def run_agent(config, directory, stopped):
    sampler = Sampler()
    inbox, media_refresh = Queue(), Event()
    listener = Thread(target=listen_commands, args=(config, stopped, inbox), name="vitals-command-stream", daemon=True)
    # AppleScript must run on the main thread. Warm the scripting engine so
    # the first wall-clock delay is paid at startup instead of on a command.
    quick = QuickControls()
    try:
        quick.volume()
    except ScriptError as exc:
        logger.debug("AppleScript 预热失败：%s", exc)
    with (closing(Caffeinate()) as caffeinate, config.client(timeout=3) as client,
          ThreadPoolExecutor(max_workers=3) as workers):
        publisher = SnapshotPublisher(client, config, caffeinate, inbox)
        workers.submit(hardware_heartbeats, publisher, sampler, listener, stopped)
        workers.submit(refresh_media, publisher, media_refresh, stopped)
        workers.submit(sync_reminders, config, directory, stopped)
        try:
            command_worker(client, config, directory, caffeinate, stopped, inbox, quick, publisher, media_refresh)
        finally:
            stopped.set()
            media_refresh.set()


def desktop_config(source):
    line = source.readline(16_384)
    if not line.endswith(b"\n"):
        raise ValueError("无法读取 Vitals 桌面代理设置")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    data["device_name"] = socket.gethostname().strip().removesuffix(".local")[:200] or "Mac"
    return Config.from_data(data)


def watch_desktop(source, stopped):
    try:
        os.read(source.fileno(), 1)
    finally:
        stopped.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--sample", action="store_true", help="采集一次本机快照，不联网")
    modes.add_argument("--sync-reminders-once", action="store_true", help="同步提醒事项一次")
    modes.add_argument("--check-config", action="store_true", help="校验配置，不联网")
    modes.add_argument("--desktop-stdio", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # HTTP client debug traces add no information needed for routine agent diagnostics.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform != "darwin":
        parser.exit(1, "Vitals Mac agent 需要 macOS\n")
    try:
        if args.sample:
            print(json.dumps({"caffeinate": False, **Sampler(allow_artwork_download=False).sample()}, ensure_ascii=False, indent=2))
            return 0
        config = desktop_config(sys.stdin.buffer) if args.desktop_stdio else Config.load(args.config)
        if args.check_config:
            return 0
        directory = state_directory(config)
        with single_instance(directory):
            if args.sync_reminders_once:
                with config.client() as client:
                    sync_once(client, config, directory)
            else:
                stopped = Event()
                for signal_type in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(signal_type, lambda *_: stopped.set())
                if args.desktop_stdio:
                    Thread(target=watch_desktop, args=(sys.stdin.buffer, stopped), daemon=True).start()
                logger.info("Vitals Mac agent 已启动 (%s)", config.device_id)
                run_agent(config, directory, stopped)
    except FAILURES as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
