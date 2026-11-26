"""Read Mac telemetry and execute the fixed set of Vitals commands."""

import json
import os
import logging
import re
import subprocess
from time import monotonic

import psutil

logger = logging.getLogger(__name__)
OSASCRIPT = "/usr/bin/osascript"
PMSET = "/usr/bin/pmset"
MEDIA_ACTIONS = {"media_play_pause": "playpause", "media_next": "nextTrack", "media_previous": "previousTrack"}
POWER_ACTIONS = {"display_sleep": "displaysleepnow", "sleep": "sleepnow"}
MEDIA_SCRIPT = """
function run(args) {
    const players = [
        ["com.apple.Music", "Apple Music"],
        ["com.spotify.client", "Spotify"]
    ].flatMap(([id, player]) => {
        try {
            const app = Application(id);
            if (!app.running()) return [];
            const status = app.playerState();
            const result = {app, player, status, title: "", artist: ""};
            if (status === "playing" || status === "paused") {
                result.title = app.currentTrack.name() || "";
                result.artist = app.currentTrack.artist() || "";
            }
            return [result];
        } catch (error) {
            return [];
        }
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
    const {player, status, title, artist} = target;
    return JSON.stringify({player, status, title, artist});
}
"""


def native(*args: str, input_text: str | None = None, timeout: float = 3) -> str:
    result = subprocess.run(args, input=input_text, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, check=False, env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    if result.returncode:
        raise RuntimeError(f"{args[0].rsplit('/', 1)[-1]}: {result.stderr.strip() or result.returncode}")
    return result.stdout


def apple(script: str, *args: str) -> str:
    return native(OSASCRIPT, "-e", script, *args)


def javascript(script: str, *args: str, timeout: float = 3) -> str:
    return native(OSASCRIPT, "-l", "JavaScript", "-e", script, *args, timeout=timeout)


def parse_battery(output: str) -> dict | None:
    match = re.search(r"(\d{1,3})%;[ \t]*([^\r\n]+)", output)
    if not match:
        return None
    percent, detail = int(match[1]), match[2].lower()
    if percent > 100 or "present: false" in detail:
        return None
    if "discharging" in detail:
        state = "discharging"
    elif "not charging" in detail:
        state = "not_charging"
    elif "charging" in detail:
        state = "charging"
    elif "charged" in detail:
        state = "charged"
    else:
        state = "unknown"
    remaining = re.search(r"(\d+):([0-5]\d)\s+remaining", detail)
    return {"percent": percent, "charging": state == "charging", "state": state,
            "minutes_left": int(remaining[1]) * 60 + int(remaining[2]) if remaining else None}


def parse_volume(output: str) -> dict:
    percent = re.search(r"output volume:\s*(\d+)", output)
    muted = re.search(r"output muted:\s*(true|false)", output)
    return {"percent": int(percent[1]) if percent else None, "muted": muted[1] == "true" if muted else None}


def volume() -> dict:
    settings = parse_volume(apple("get volume settings"))
    return settings if None not in settings.values() else None


def media():
    track = json.loads(javascript(MEDIA_SCRIPT))
    if track is not None:
        track["title"] = track["title"][:300]
        track["artist"] = track["artist"][:300]
        if track["status"] not in {"playing", "paused", "stopped"}:
            track["status"] = "unknown"
    return track


def execute_command(action: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    if action == "volume_set":
        percent = params.get("percent")
        if params.keys() != {"percent"} or type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("volume_set 需要 0–100 的整数 percent")
        apple(f"set volume output volume {percent}")
    elif action == "volume_mute":
        muted = params.get("muted")
        if params.keys() != {"muted"} or type(muted) is not bool:
            raise ValueError("volume_mute 需要布尔值 muted")
        apple("set volume with output muted" if muted else "set volume without output muted")
    elif params:
        raise ValueError(f"{action} 不接受参数")
    elif action in MEDIA_ACTIONS:
        javascript(MEDIA_SCRIPT, MEDIA_ACTIONS[action])
    elif action in POWER_ACTIONS:
        native(PMSET, POWER_ACTIONS[action])
    elif action == "lock":
        apple('tell application "System Events" to keystroke "q" using {control down, command down}')
    else:
        raise ValueError(f"不支持的指令 {action}")


def top_memory_processes() -> list[dict]:
    processes = []
    for process in psutil.process_iter(["pid", "name", "memory_info"], ad_value=None):
        try:
            info = process.info
            if info["pid"] and info["name"] and info["memory_info"] and info["memory_info"].rss:
                processes.append({"pid": info["pid"], "name": info["name"][:200], "rss_bytes": info["memory_info"].rss})
        except (OSError, psutil.Error):
            continue
    return sorted(processes, key=lambda entry: (-entry["rss_bytes"], entry["pid"]))[:5]


def read_optional(label, read):
    try:
        return read()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("%s unavailable: %s", label, exc)
        return None


class Sampler:
    def __init__(self):
        self.previous_net = None
        self.cpu_name = read_optional("CPU name", lambda: native("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string").strip())
        psutil.cpu_percent()

    def sample(self):
        net = psutil.net_io_counters()
        now = monotonic()
        sent_rate = received_rate = None
        if net is not None and self.previous_net:
            previous, sampled_at = self.previous_net
            if now > sampled_at:
                sent_rate = round(max(0, net.bytes_sent - previous.bytes_sent) / (now - sampled_at), 1)
                received_rate = round(max(0, net.bytes_recv - previous.bytes_recv) / (now - sampled_at), 1)
        self.previous_net = (net, now) if net is not None else None
        memory = psutil.virtual_memory()
        disks = []
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (OSError, psutil.Error):
                continue
            if len(partition.mountpoint) > 200:
                continue
            disks.append({"device": partition.device, "mountpoint": partition.mountpoint, "free_bytes": usage.free,
                          "total_bytes": usage.total, "percent": usage.percent})
        return {
            "cpu": {"name": self.cpu_name, "physical_cores": psutil.cpu_count(logical=False),
                    "logical_cores": psutil.cpu_count(), "usage_percent": psutil.cpu_percent()},
            "memory": {"used_bytes": memory.total - memory.available, "available_bytes": memory.available,
                       "total_bytes": memory.total, "percent": memory.percent},
            "top_processes": top_memory_processes(),
            "disks": disks[:48],
            "network": {"total_upload_bytes": net.bytes_sent if net else None,
                        "total_download_bytes": net.bytes_recv if net else None,
                        "upload_bytes_per_sec": sent_rate, "download_bytes_per_sec": received_rate},
            "battery": read_optional("battery", lambda: parse_battery(native(PMSET, "-g", "batt"))),
            "volume": read_optional("volume", volume),
            "media": read_optional("media", media),
        }
