"""Run the Vitals Mac agent in the logged-in user's session."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, time

import httpx
import psutil

if __package__ in ("", None):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac_agent.api import CONFIG_PATH, Config
from mac_agent.clipboard import notify, push
from mac_agent.macos import Caffeinate, Sampler, execute_command
from mac_agent.reminders import sync_once
from mac_agent.state import load_state, save_state, single_instance, state_directory

HEARTBEAT_SECONDS = 3
REMINDERS_SECONDS = 30
STREAM_RETRY_SECONDS = 2
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
RECEIPT_TTL = 48 * 3600
FAILURES = (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError, KeyError, psutil.Error,
            subprocess.TimeoutExpired)
logger = logging.getLogger(__name__)


def heartbeat(client, config, sampler, caffeinate):
    path = f"/api/agents/{config.device_id}/heartbeat"
    snapshot = sampler.sample()
    response = client.post(path, json={
        "name": config.device_name, "os": "macos", "timestamp": datetime.now(timezone.utc).isoformat(),
        "caffeinate": caffeinate.active(), **snapshot,
    })
    response.raise_for_status()
    data = response.json()
    commands = data["commands"]
    if not isinstance(commands, list):
        raise ValueError("心跳响应中的 commands 必须是数组")
    if data.get("artwork_needed") is True and snapshot["media"]:
        version = snapshot["media"]["artwork_version"]
        contents = sampler.playback.bytes_for(version)
        if contents:
            try:
                upload = client.post(f"/api/agents/{config.device_id}/artwork", params={"version": version},
                                     headers={"Content-Type": "image/jpeg"}, content=contents)
                upload.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("封面上传失败：%s", exc)
    return commands


def command_events(lines):
    event = None
    data = []
    for line in lines:
        if not line:
            if event == "command" and data:
                command = json.loads("\n".join(data))
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
    with config.client(timeout=httpx.Timeout(5, read=26)) as client:
        while not stopped.is_set():
            try:
                with client.stream("GET", path, headers={"Accept": "text/event-stream"}) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise ValueError("指令流响应格式不正确")
                    for command in command_events(response.iter_lines()):
                        if stopped.is_set():
                            return
                        while not stopped.is_set():
                            try:
                                inbox.put([command], timeout=0.5)
                                break
                            except Full:
                                continue
            except (httpx.HTTPError, ValueError) as exc:
                # 旧中心端或正在重启的中心端继续通过心跳下发。
                logger.debug("指令流暂不可用（%s）", type(exc).__name__)
            stopped.wait(STREAM_RETRY_SECONDS)


def process_commands(client, config, commands, directory, caffeinate):
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

    changed = False
    retried = [(key, value) for key, value in receipts.items() if not value["reported"]]
    for command in commands:
        if not isinstance(command, dict):
            continue
        command_id = command.get("id")
        if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            logger.warning("忽略没有有效 ID 的指令")
            continue
        if command_id in receipts:
            continue
        receipt = {"time": time(), "reported": False,
                   "result": {"ok": False, "message": "执行时进程中断，结果未知"}}
        receipts[command_id] = receipt
        save_state(path, receipts)
        try:
            action = command.get("action")
            if not isinstance(action, str):
                raise ValueError("action 必须是字符串")
            execute_command(action, command.get("payload", {}), caffeinate=caffeinate,
                            clipboard_push=send_clipboard)
            receipt["result"] = {"ok": True, "message": "已完成"}
            changed = True
        except FAILURES as exc:
            receipt["result"] = {"ok": False, "message": str(exc)[:500]}
        save_state(path, receipts)
        report(command_id, receipt)
    for command_id, receipt in retried:
        report(command_id, receipt)
    return changed


def command_worker(config, directory, caffeinate, stopped, inbox, sample_now):
    with config.client() as client:
        while not stopped.is_set():
            try:
                commands = inbox.get(timeout=HEARTBEAT_SECONDS)
            except Empty:
                commands = []
            if stopped.is_set():
                break
            try:
                if process_commands(client, config, commands, directory, caffeinate):
                    sample_now.set()
            except FAILURES as exc:
                logger.warning("处理指令失败：%s", exc)


def run_agent(config, directory, stopped):
    sampler = Sampler()
    next_heartbeat = next_reminders = monotonic()
    reminders_task = None
    inbox = Queue(maxsize=64)
    sample_now = Event()
    listener = Thread(target=listen_commands, args=(config, stopped, inbox), name="vitals-command-stream", daemon=True)
    with (closing(Caffeinate()) as caffeinate, config.client(timeout=3) as client, config.client() as reminders_client,
          ThreadPoolExecutor(max_workers=2) as workers):
        workers.submit(command_worker, config, directory, caffeinate, stopped, inbox, sample_now)
        try:
            while not stopped.is_set():
                task = reminders_task
                if task is not None and task.done():
                    try:
                        task.result()
                    except FAILURES as exc:
                        logger.warning("提醒事项失败：%s", exc)
                    reminders_task = None
                now = monotonic()
                if now >= next_reminders:
                    next_reminders = now + REMINDERS_SECONDS
                    if reminders_task is None:
                        reminders_task = workers.submit(sync_once, reminders_client, config, directory)
                if now >= next_heartbeat or sample_now.is_set():
                    sample_now.clear()
                    next_heartbeat = now + HEARTBEAT_SECONDS
                    try:
                        commands = heartbeat(client, config, sampler, caffeinate)
                        if commands:
                            inbox.put(commands)
                        if listener.ident is None:
                            listener.start()
                    except FAILURES as exc:
                        logger.warning("心跳失败：%s", exc)
                sample_now.wait(max(0, min(next_heartbeat, next_reminders) - monotonic()))
        finally:
            stopped.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--sample", action="store_true", help="采集一次本机快照，不联网")
    modes.add_argument("--sync-reminders-once", action="store_true", help="同步提醒事项一次")
    modes.add_argument("--check-config", action="store_true", help="校验配置，不联网")
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
        config = Config.load(args.config)
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
                logger.info("Vitals Mac agent 已启动 (%s)", config.device_id)
                run_agent(config, directory, stopped)
    except FAILURES as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
