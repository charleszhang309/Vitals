"""Read Mac telemetry and execute the fixed set of Vitals commands."""

from ipaddress import ip_address, ip_network
import json
import logging
import os
import plistlib
import re
import socket
import subprocess
from threading import Lock
from time import monotonic

import psutil

from .native import apple, javascript, native
from .playback import MEDIA_ACTIONS, MEDIA_SCRIPT, Playback

logger = logging.getLogger(__name__)
PMSET = "/usr/bin/pmset"
POWER_ACTIONS = {"display_lock": "displaysleepnow", "sleep": "sleepnow",
                 "lock": "displaysleepnow", "display_sleep": "displaysleepnow"}
TAILNETS = (ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48"))
AUDIO_PATHS = ("/opt/homebrew/bin/SwitchAudioSource", "/usr/local/bin/SwitchAudioSource")


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


def audio_tool():
    return next((path for path in AUDIO_PATHS if os.path.isfile(path) and os.access(path, os.X_OK)), None)


def current_output(tool):
    return json.loads(native(tool, "-c", "-t", "output", "-f", "json")).get("uid")


def audio_outputs(tool):
    rows = native(tool, "-a", "-t", "output", "-f", "json").splitlines()
    devices = {}
    for row in rows:
        if not row.strip():
            continue
        item = json.loads(row)
        uid, name, number = item.get("uid"), item.get("name"), str(item.get("id", ""))
        if (isinstance(uid, str) and 0 < len(uid) <= 256 and isinstance(name, str)
                and name.strip() and number.isascii() and number.isdigit()):
            devices[uid] = (name.strip()[:200], number)
    return devices


def volume() -> dict | None:
    settings = parse_volume(apple("get volume settings"))
    tool = audio_tool()
    devices = (read_optional("audio output devices", lambda: audio_outputs(tool)) or {}) if tool else {}
    selected = read_optional("current audio output", lambda: current_output(tool)) if devices else None
    if not devices and None in settings.values():
        return None
    return {**settings, "device_id": selected,
            "devices": [{"id": uid, "name": value[0]} for uid, value in list(devices.items())[:64]]}


def set_audio_output(params):
    uid = params.get("device_id")
    if params.keys() != {"device_id"} or not isinstance(uid, str) or not 0 < len(uid) <= 256:
        raise ValueError("audio_device_set 需要有效的 device_id")
    tool = audio_tool()
    if tool is None:
        raise RuntimeError("未安装 SwitchAudioSource")
    device = audio_outputs(tool).get(uid)
    if device is None:
        raise ValueError("找不到该音频输出设备")
    native(tool, "-t", "output", "-i", device[1])
    if current_output(tool) != uid:
        raise RuntimeError("系统未切换到所选音频输出设备")


class Caffeinate:
    def __init__(self):
        self.process = None
        self.lock = Lock()

    def active(self):
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def _stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def toggle(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self._stop()
            else:
                self.process = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-di", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

    def close(self):
        with self.lock:
            self._stop()


def execute_command(action: str, params: dict, *, caffeinate=None, clipboard_push=None, quick=None):
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    if action == "volume_set":
        percent = params.get("percent")
        if params.keys() != {"percent"} or type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("volume_set 需要 0–100 的整数 percent")
        script = f"set volume output volume {percent}"
        if quick:
            return {"volume": quick.volume(percent=percent)}
        apple(script)
        return {"volume": parse_volume(apple("get volume settings"))}
    elif action == "volume_mute":
        muted = params.get("muted")
        if params.keys() != {"muted"} or type(muted) is not bool:
            raise ValueError("volume_mute 需要布尔值 muted")
        script = "set volume with output muted" if muted else "set volume without output muted"
        if quick:
            return {"volume": quick.volume(muted=muted)}
        apple(script)
        return {"volume": parse_volume(apple("get volume settings"))}
    elif action == "audio_device_set":
        set_audio_output(params)
        if quick:
            return {"volume": {**quick.volume(), "device_id": params["device_id"]}}
        return {"volume": {**parse_volume(apple("get volume settings")), "device_id": params["device_id"]}}
    elif params:
        raise ValueError(f"{action} 不接受参数")
    elif action in MEDIA_ACTIONS:
        if quick:
            quick.media(action)
        else:
            javascript(MEDIA_SCRIPT, MEDIA_ACTIONS[action])
    elif action in POWER_ACTIONS:
        native(PMSET, POWER_ACTIONS[action])
    elif action == "caffeinate_toggle" and caffeinate is not None:
        caffeinate.toggle()
        return {"caffeinate": caffeinate.active()}
    elif action == "clipboard_push" and clipboard_push is not None:
        clipboard_push()
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
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, plistlib.InvalidFileException) as exc:
        logger.debug("%s unavailable: %s", label, exc)
        return None


def external_disk(mountpoint):
    info = plistlib.loads(native("/usr/sbin/diskutil", "info", "-plist", mountpoint, timeout=2).encode())
    protocol = info.get("BusProtocol", "").casefold()
    return (info.get("Internal") is False and bool(protocol)
            and not any(kind in protocol for kind in ("disk image", "virtual")))


class Disks:
    def __init__(self):
        self.external = {}

    def read(self):
        disks = []
        candidates = {}
        now = monotonic()
        for part in psutil.disk_partitions():
            mount = part.mountpoint
            if mount != "/":
                if (not re.fullmatch(r"/Volumes/[^/]+", mount) or mount.casefold() == "/volumes/recovery"
                        or len(mount) > 200 or not re.fullmatch(r"/dev/disk\d+(?:s\d+)*", part.device)):
                    continue
                key = (part.device, mount)
                valid, expiry = self.external.get(key, (False, 0))
                if now >= expiry:
                    valid = read_optional("external disk", lambda: external_disk(mount))
                    expiry = now + 30
                candidates[key] = valid, expiry
                if not valid:
                    continue
            try:
                usage = psutil.disk_usage(mount)
            except (OSError, psutil.Error):
                continue
            disks.append({"name": "Macintosh HD" if mount == "/" else mount.removeprefix("/Volumes/"),
                          "mountpoint": mount, "free_bytes": usage.free, "total_bytes": usage.total})
        self.external = candidates
        return sorted(disks, key=lambda disk: (disk["mountpoint"] != "/", disk["mountpoint"]))[:48]


def battery_details():
    info = read_optional("battery registry", lambda: plistlib.loads(
        native("/usr/sbin/ioreg", "-r", "-n", "AppleSmartBattery", "-a").encode())) or []
    data = info[0] if info else {}
    profiler = read_optional("battery profiler", lambda: json.loads(
        native("/usr/sbin/system_profiler", "SPPowerDataType", "-json", timeout=4))) or {}
    health = next((item["sppower_battery_health_info"] for item in profiler.get("SPPowerDataType", [])
                   if "sppower_battery_health_info" in item), {})
    cycles = health.get("sppower_battery_cycle_count", data.get("CycleCount"))
    capacity = re.fullmatch(r"(\d{1,3})\s*%", str(health.get("sppower_battery_health_maximum_capacity", "")))
    if capacity:
        percent = min(100, int(capacity[1]))
    else:
        full, design = data.get("AppleRawMaxCapacity"), data.get("DesignCapacity")
        percent = round(min(100, 100 * full / design), 1) if full and design else None
    raw = data.get("Temperature")
    temperature = round(raw / 10 - 273.15, 1) if isinstance(raw, (float, int)) and 2300 <= raw <= 3700 else None
    return {"cycle_count": cycles if type(cycles) is int and cycles >= 0 else None,
            "health_percent": percent, "temperature_c": temperature}


def active_interfaces():
    stats = psutil.net_if_stats()
    interfaces = []
    for name, records in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        addresses = []
        for record in records:
            if record.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                address = ip_address(record.address.split("%", 1)[0])
            except ValueError:
                continue
            if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                addresses.append(address)
        if addresses:
            tailscale = (name.startswith("utun") or "tailscale" in name.casefold()) and any(
                ip in network for ip in addresses for network in TAILNETS)
            interfaces.append({"name": name, "addresses": [str(ip) for ip in addresses[:16]], "tailscale": tailscale})
    return interfaces[:32]


class Sampler:
    def __init__(self, *, allow_artwork_download=True):
        self.disks = Disks()
        self.playback = Playback(allow_download=allow_artwork_download)
        self.battery_health = {}
        self.battery_expires = 0
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
        battery = read_optional("battery", lambda: parse_battery(native(PMSET, "-g", "batt")))
        if battery is not None:
            if now >= self.battery_expires:
                self.battery_health = battery_details()
                self.battery_expires = now + 300
            battery.update(self.battery_health)
        return {
            "cpu": {"name": self.cpu_name, "physical_cores": psutil.cpu_count(logical=False),
                    "logical_cores": psutil.cpu_count(), "usage_percent": psutil.cpu_percent()},
            "memory": {"used_bytes": memory.total - memory.available, "available_bytes": memory.available,
                       "total_bytes": memory.total, "percent": memory.percent},
            "top_processes": top_memory_processes(),
            "disks": self.disks.read(),
            "network": {"total_upload_bytes": net.bytes_sent if net else None,
                        "total_download_bytes": net.bytes_recv if net else None,
                        "upload_bytes_per_sec": sent_rate, "download_bytes_per_sec": received_rate,
                        "interfaces": active_interfaces()},
            "battery": battery,
            "volume": read_optional("volume", volume),
            "media": read_optional("media", self.playback.sample),
        }
