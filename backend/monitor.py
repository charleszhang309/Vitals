"""采集硬件快照、判断任务和告警，并异步发送通知。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
import logging
import math
from pathlib import Path
import platform
import re
import socket
import sys
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx
import psutil

from .config import GIB, MIB, SamplingMode, Settings, WebhookConfig


logger = logging.getLogger(__name__)
REALTIME_INTERVAL = 1.0
ECO_INTERVAL = 5.0
RULES_GAP_SECONDS = 10.0
LHM_RETRY_SECONDS = 5.0
NUMBER = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)*)\s*(.*?)\s*$")
GROUPED_COMMAS = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+")
MEMORY_UNITS = {"b": 1, "kb": 1024, "kib": 1024, "mb": MIB, "mib": MIB, "gb": GIB, "gib": GIB, "tb": 1024 * GIB}
CPU_KINDS = {"amdcpu", "intelcpu", "genericcpu", "cpu", "cpu-amd", "cpu-intel"}
CORE = re.compile(r"^((?:cpu )?core|[pe]-core)\s*#?\s*(\d+)(?:\s*\[?thread\s*#?\s*\d+\]?)?$", re.IGNORECASE)
FAN_NUMBER = re.compile(r"^fan\s*#?\s*(\d+)$", re.IGNORECASE)
GPU_KINDS = {
    "nvidiagpu", "atigpu", "amdgpu", "intelgpu", "gpu", "gpu-nvidia", "gpu-amd",
    "gpu-intel", "gpu-intel-integrated", "gpu-intel-discrete",
}
IDLE_JOB = {"state": "idle", "source": None, "started_at": None, "completed_at": None}
Snapshot = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SystemSampler:
    def __init__(self):
        self.previous_cpu = None
        self.previous_net = None
        self.cpu_identity = {"name": system_cpu_name(), "physical_cores": psutil.cpu_count(logical=False),
                             "logical_cores": psutil.cpu_count()}
        self.details_at = -math.inf
        self.processes = []
        self.interfaces = []

    def sample(self) -> Snapshot:
        current_cpu = psutil.cpu_times()._asdict()
        cpu_percent = None
        if self.previous_cpu is not None:
            delta = {key: max(0, value - self.previous_cpu[key]) for key, value in current_cpu.items()}
            total = sum(delta.values()) - delta.get("guest", 0) - delta.get("guest_nice", 0)
            busy = total - delta["idle"] - delta.get("iowait", 0)
            if total > 0:
                cpu_percent = round(min(100, max(0, 100 * busy / total)), 1)
        self.previous_cpu = current_cpu

        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        net = psutil.net_io_counters()
        now = monotonic()
        rates = {"upload_bytes_per_sec": None, "download_bytes_per_sec": None}
        if net is not None and self.previous_net is not None:
            previous, previous_time = self.previous_net
            elapsed = now - previous_time
            if elapsed > 0:
                rates = {
                    "upload_bytes_per_sec": round(max(0, net.bytes_sent - previous.bytes_sent) / elapsed, 1),
                    "download_bytes_per_sec": round(max(0, net.bytes_recv - previous.bytes_recv) / elapsed, 1),
                }
        self.previous_net = (net, now) if net is not None else None
        if now - self.details_at >= 6:
            self.processes = top_memory_processes()
            self.interfaces = active_interfaces()
            self.details_at = now

        disks = []
        for partition in psutil.disk_partitions():
            if "cdrom" in partition.opts.split(","):
                continue
            try:
                usage = psutil.disk_usage(str(Path(partition.mountpoint)))
            except (OSError, psutil.Error):
                # U 盘拔出或分区权限不足时继续采集其他分区。
                continue
            disks.append({
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "percent": usage.percent,
            })
        return {
            "cpu": {**self.cpu_identity, "usage_percent": cpu_percent, "temperature_c": None, "power_w": None},
            "memory": {"used_bytes": memory.total - memory.available, "available_bytes": memory.available,
                       "total_bytes": memory.total, "percent": memory.percent, "top_processes": self.processes,
                       "virtual": {"used_bytes": swap.used, "available_bytes": swap.free,
                                   "total_bytes": swap.total, "percent": swap.percent}},
            "disks": disks,
            "network": {**rates, "interfaces": self.interfaces, "total_upload_bytes": net.bytes_sent if net else None,
                        "total_download_bytes": net.bytes_recv if net else None},
        }


class MacSystemSampler:
    def __init__(self):
        from mac_agent.macos import Sampler

        self.native = Sampler()

    def sample(self) -> Snapshot:
        sample = self.native.sample_hardware()
        swap = psutil.swap_memory()
        sample["cpu"].update(temperature_c=None, power_w=None)
        sample["memory"].update(top_processes=sample.pop("top_processes"), virtual={
            "used_bytes": swap.used, "available_bytes": swap.free, "total_bytes": swap.total, "percent": swap.percent,
        })
        for disk in sample["disks"]:
            total = disk["total_bytes"]
            disk["percent"] = round(100 * (total - disk["free_bytes"]) / total, 1) if total else 0
        return sample


def host_identity():
    platform_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    return {"platform": platform_name, "name": socket.gethostname()}


def system_cpu_name() -> str | None:
    if platform.system() == "Windows":
        from winreg import HKEY_LOCAL_MACHINE, OpenKey, QueryValueEx

        try:
            with OpenKey(HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return QueryValueEx(key, "ProcessorNameString")[0].strip() or None
        except OSError:
            return None
    return platform.processor() or None


def top_memory_processes():
    processes = []
    for process in psutil.process_iter(["pid", "name", "memory_info"], ad_value=None):
        try:
            info = process.info
            if info["name"] and info["memory_info"] and info["pid"] and info["memory_info"].rss > 0:
                processes.append({"pid": info["pid"], "name": info["name"], "rss_bytes": info["memory_info"].rss})
        except (psutil.Error, OSError):
            continue
    return sorted(processes, key=lambda process: (-process["rss_bytes"], process["pid"]))[:5]


def active_interfaces():
    stats = psutil.net_if_stats()
    result = []
    for name, addresses in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        ips = []
        tailscale = "tailscale" in name.casefold()
        for address in addresses:
            if address.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                ip = ip_address(address.address.split("%", 1)[0])
            except ValueError:
                continue
            if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
                ips.append(str(ip))
        if ips:
            result.append({"name": name, "addresses": ips, "tailscale": tailscale})
    return result


@dataclass(frozen=True)
class Sensor:
    id: str
    label: str
    kind: str
    value: float


def read_sensor(node: dict, sensor_id: str) -> Sensor | None:
    label = str(node.get("Text", ""))
    kind = str(node.get("Type") or sensor_id.split("/")[-2]).lower()
    raw = node.get("RawValue")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # 新版原始数据为字节，旧版返回带 GB 或 MB 的字符串。
        value = float(raw)
        unit = "mb" if kind == "smalldata" else "b"
    else:
        formatted = NUMBER.match(str(raw)) or NUMBER.match(str(node.get("Value", "")))
        if formatted is None:
            return None
        number = formatted[1]
        if "," in number and "." in number:
            if number.rfind(",") > number.rfind("."):
                number = number.replace(".", "").replace(",", ".")
            else:
                number = number.replace(",", "")
        elif GROUPED_COMMAS.fullmatch(number):
            number = number.replace(",", "")
        else:
            number = number.replace(",", ".")
        try:
            value = float(number)
        except ValueError:
            return None
        unit = formatted[2].lower()
    if kind in {"data", "smalldata"}:
        value *= MEMORY_UNITS.get(unit, MIB if kind == "smalldata" else GIB)
    elif kind == "temperature" and unit in {"°f", "° f", "f"}:
        value = (value - 32) * 5 / 9
    return Sensor(sensor_id, label, kind, value) if math.isfinite(value) else None


def select(sensors: list[Sensor], kinds: set[str], *labels: str) -> float | None:
    for label in labels:
        for sensor in sensors:
            if sensor.kind in kinds and sensor.label.casefold() == label.casefold():
                return sensor.value
    return None


def parse_cpu(groups, names):
    sources = {}
    clocks, voltages, core_temperatures = [], [], []
    cores = {}
    loads = []
    powers = []
    for device, sensors in groups.items():
        get = lambda kind, *labels: select(sensors, {kind}, *labels)
        package = get("temperature", "CPU Package", "Core (Tctl/Tdie)", "CPU (Tctl/Tdie)", "Core (Tdie)")
        core_max = get("temperature", "Core Max", "CPU Core Max")
        power = get("power", "CPU Package", "Package", "CPU Package Power", "CPU PPT")
        load = get("load", "CPU Total", "Total CPU", "CPU")
        voltage = get("voltage", "CPU Core", "CPU VCore", "Core (SVI3 TFN)", "Core (SVI2 TFN)", "Core Voltage")
        device_temperatures, device_voltages = [], []
        for sensor in sensors:
            match = CORE.fullmatch(sensor.label)
            if not match:
                continue
            if sensor.kind == "load":
                # LHM 的 SMT 核心可能按线程报告，使用同一物理核心的线程均值。
                key = (device, int(match[2]), match[1].casefold().removeprefix("cpu "))
                label, values = cores.setdefault(key, (f"{match[1]} #{match[2]}", []))
                values.append(sensor.value)
            elif sensor.kind == "clock":
                clocks.append(sensor.value)
            elif sensor.kind == "temperature":
                device_temperatures.append(sensor.value)
            elif sensor.kind == "voltage":
                device_voltages.append(sensor.value)
        core_max = core_max if core_max is not None else max(device_temperatures, default=None)
        sources[device] = (package, core_max)
        if core_max is not None:
            core_temperatures.append(core_max)
        if voltage is not None:
            voltages.append(voltage)
        elif device_voltages:
            voltages.append(sum(device_voltages) / len(device_voltages))
        if load is not None:
            loads.append(load)
        if power is not None:
            powers.append(power)
    temperatures = {device: package if package is not None else core_max
                    for device, (package, core_max) in sources.items() if package is not None or core_max is not None}
    packages = [package for package, _ in sources.values() if package is not None]
    return {
        "name": " / ".join(dict.fromkeys(names[device] for device in groups)) or None,
        "usage_percent": sum(loads) / len(loads) if loads else None,
        "temperature_c": max(temperatures.values(), default=None),
        "package_temperature_c": max(packages, default=None),
        "max_core_temperature_c": max(core_temperatures, default=None),
        "power_w": round(sum(powers), 2) if powers else None,
        "average_clock_mhz": sum(clocks) / len(clocks) if clocks else None,
        "max_clock_mhz": max(clocks, default=None),
        "voltage_v": max(voltages, default=None),
        "cores": [{"id": f"{device}:{kind}:{index}", "name": label, "usage_percent": sum(values) / len(values)}
                  for (device, index, kind), (label, values) in sorted(cores.items())],
    }, temperatures


def parse_memory(sensors):
    kinds = {"data", "smalldata"}
    used = select(sensors, kinds, "Memory Used", "Virtual Memory Used", "Physical Memory Used")
    available = select(sensors, kinds, "Memory Available", "Virtual Memory Available", "Memory Free")
    total = select(sensors, kinds, "Memory Total", "Total Memory", "Virtual Memory Total")
    percent = select(sensors, {"load"}, "Memory", "Virtual Memory", "Physical Memory")
    used, available, total = (round(value) if value is not None and value >= 0 else None for value in (used, available, total))
    if total is None and used is not None and available is not None:
        total = used + available
    if used is None and total is not None and available is not None and available <= total:
        used = total - available
    if available is None and total is not None and used is not None and used <= total:
        available = total - used
    if percent is None and used is not None and total:
        percent = 100 * used / total
    return {key: value for key, value in {"used_bytes": used, "available_bytes": available, "total_bytes": total,
                                          "percent": percent}.items() if value is not None}


def parse_fans(device, sensors, name, mapping):
    gpu = device.lstrip("/").split("/", 1)[0].lower() in GPU_KINDS
    controls = {sensor.label.casefold(): sensor.value for sensor in sensors if sensor.kind == "control"}
    for sensor in sensors:
        if sensor.kind != "fan" or round(sensor.value) <= 0:
            continue
        label = sensor.label.casefold()
        number = FAN_NUMBER.fullmatch(label)
        if gpu:
            group = "gpu"
        else:
            group = mapping.get(sensor.id) or mapping.get(label) or (mapping.get(number[1]) if number else None)
            group = group or ("cpu" if "cpu" in label or device.lstrip("/").split("/", 1)[0].lower() in CPU_KINDS else "chassis")
        duty = controls.get(label)
        yield {"id": sensor.id, "name": sensor.label, "device": name, "rpm": round(sensor.value), "group": group,
               "percent": duty if duty is not None and 0 <= duty <= 100 else None}


def parse_lhm(tree: dict, fan_mapping: dict | None = None) -> Snapshot:
    if not isinstance(tree, dict) or not isinstance(tree.get("Children"), list):
        raise ValueError("LHM 响应缺少传感器树")
    hardware_names: dict[str, str] = {}
    groups: dict[str, list[Sensor]] = {}
    stack = [(tree, "", "")]
    while stack:
        node, hardware_id, hardware_name = stack.pop()
        if not isinstance(node, dict):
            continue
        children = node.get("Children")
        icon = str(node.get("ImageURL", "")).rsplit("/", 1)[-1]
        explicit_id = node.get("HardwareId")
        if isinstance(explicit_id, str) or icon in {"cpu.png", "nvidia.png", "ati.png", "intel.png", "gpu.png", "mainboard.png", "chip.png", "ram.png"}:
            hardware_id = explicit_id if isinstance(explicit_id, str) else ""
            hardware_name = str(node.get("Text", ""))
        if isinstance(children, list):
            stack.extend((child, hardware_id, hardware_name) for child in reversed(children))
        sensor_id = node.get("SensorId")
        if not isinstance(sensor_id, str) or len(sensor_id.strip("/").split("/")) < 3:
            continue
        sensor = read_sensor(node, sensor_id)
        if sensor is None:
            continue
        device = hardware_id or sensor_id.rsplit("/", 2)[0]
        hardware_names.setdefault(device, hardware_name or device)
        groups.setdefault(device, []).append(sensor)

    mapping = {key.strip().casefold(): value for key, value in (fan_mapping or {}).items()}
    fans = []
    cpu_groups = {}
    gpus = []
    for device, sensors in groups.items():
        device_kind = device.lstrip("/").split("/", 1)[0].lower()
        device_fans = list(parse_fans(device, sensors, hardware_names[device], mapping))
        fans.extend(device_fans)
        if device_kind in CPU_KINDS:
            cpu_groups[device] = sensors
        if device_kind in GPU_KINDS:
            kinds = {"data", "smalldata"}
            used = select(sensors, kinds, "GPU Memory Used", "D3D Dedicated Memory Used", "GPU Dedicated Memory Used")
            total = select(sensors, kinds, "GPU Memory Total", "D3D Dedicated Memory Total", "GPU Dedicated Memory Total")
            free = select(sensors, kinds, "GPU Memory Free", "D3D Dedicated Memory Free", "GPU Dedicated Memory Free")
            if total is None and used is not None and free is not None:
                total = used + free
            if used is None and total is not None and free is not None:
                used = max(0, total - free)
            gpus.append({
                "id": device,
                "name": hardware_names[device],
                "usage_percent": select(sensors, {"load"}, "GPU Core", "GPU Total", "GPU 3D", "D3D 3D", "3D", "GPU Render/Compute"),
                "temperature_c": select(sensors, {"temperature"}, "GPU Core", "GPU Temperature"),
                "hotspot_temperature_c": select(sensors, {"temperature"}, "GPU Hot Spot", "GPU Hotspot", "GPU Junction"),
                "core_clock_mhz": select(sensors, {"clock"}, "GPU Core", "GPU Clock"),
                "memory_clock_mhz": select(sensors, {"clock"}, "GPU Memory", "GPU Memory Clock", "GPU VRAM"),
                "power_w": select(sensors, {"power"}, "GPU Package", "GPU Board Power", "GPU Power", "GPU PPT", "GPU Core"),
                "vram_used_bytes": round(used) if used is not None else None,
                "vram_total_bytes": round(total) if total is not None else None,
                "fans": device_fans,
            })
    cpu, cpu_temperatures = parse_cpu(cpu_groups, hardware_names)
    summary = []
    for group in ("cpu", "gpu", "chassis"):
        active = max((fan for fan in fans if fan["group"] == group), key=lambda fan: fan["rpm"], default=None)
        if active:
            summary.append({"id": group, "group": group, "rpm": active["rpm"], "percent": active["percent"]})
    cpu["fan_rpm"] = next((fan["rpm"] for fan in summary if fan["group"] == "cpu"), None)
    cpu["pump_rpm"] = max((fan["rpm"] for fan in fans if fan["group"] == "pump"), default=None)
    return {
        "cpu": cpu,
        "cpu_temperatures": cpu_temperatures,
        "cpu_power_w": cpu["power_w"],
        "memory": parse_memory(groups.get("/ram", [])),
        "virtual_memory": parse_memory(groups.get("/vram", [])),
        "gpus": gpus,
        "fans": fans,
        "fan_summary": summary,
    }


def merge_hardware(sample, hardware):
    sample.update(lhm_connected=hardware is not None, gpus=[], fans=[], fan_summary=[])
    if hardware is None:
        return {}
    sample["cpu"].update({key: value for key, value in hardware["cpu"].items() if value is not None})
    sample["memory"].update(hardware["memory"])
    if hardware["virtual_memory"]:
        sample["memory"]["virtual"] = {**hardware["virtual_memory"], "source": "lhm"}
    sample.update(gpus=hardware["gpus"], fans=hardware["fans"], fan_summary=hardware["fan_summary"])
    return hardware["cpu_temperatures"]


class Rules:
    def __init__(self, settings: Settings):
        self.task = settings.task
        self.alerts = settings.alerts
        self.high_since: dict[str, float] = {}
        self.hot_since: dict[tuple[str, str], float] = {}
        self.low_since: float | None = None
        self.missing_since: float | None = None
        self.job: Snapshot = IDLE_JOB.copy()
        self.last_alerts: dict[str, Snapshot] = {}
        self.last_alert_times: dict[tuple[str, str], float] = {}
        self.previous_sample: float | None = None

    def evaluate(self, sample: Snapshot, cpu_temperatures: dict[str, float], now: float) -> list[Snapshot]:
        timestamp = sample["timestamp"]
        events = []
        if self.previous_sample is not None and now - self.previous_sample > RULES_GAP_SECONDS:
            # 长时间缺样时重新计时，避免把休眠或采集停顿当作连续超阈值。
            self.high_since.clear()
            self.hot_since.clear()
            self.low_since = None
            self.missing_since = None
        self.previous_sample = now
        loads = {}
        if self.task.source != "gpu":
            loads["cpu"] = sample["cpu"]["usage_percent"]
        if self.task.source != "cpu":
            loads.update({gpu["id"]: gpu["usage_percent"] for gpu in sample["gpus"]})

        if self.job["state"] == "running":
            load = loads.get(self.job["source"])
            if load is None:
                self.low_since = None
                if self.missing_since is None:
                    self.missing_since = now
                if now - self.missing_since >= max(self.task.low_seconds, 60.0):
                    # 读数长期缺失时释放任务槽位，避免将离线误判为完工。
                    self.job = IDLE_JOB.copy()
                    self.missing_since = None
            else:
                self.missing_since = None
                if load >= self.task.low_percent:
                    self.low_since = None
                else:
                    if self.low_since is None:
                        self.low_since = now
                    if now - self.low_since >= self.task.low_seconds:
                        self.job = {**self.job, "state": "completed", "completed_at": timestamp}
                        events.append(self.event("task_completed", "任务已完工", f"{self.job['source']} 负载已持续回落。", timestamp))
                        self.low_since = None
        else:
            self.high_since = {
                source: self.high_since.get(source, now)
                for source, load in loads.items()
                if load is not None and load > self.task.high_percent
            }
            if self.high_since:
                source = min(self.high_since, key=self.high_since.__getitem__)
                if now - self.high_since[source] >= self.task.high_seconds:
                    self.job = {"state": "running", "source": source, "started_at": timestamp, "completed_at": None}
                    self.high_since.clear()

        hot = {
            ("cpu_temperature", device): value for device, value in cpu_temperatures.items()
            if value > self.alerts.cpu_temperature_c
        }
        hot.update({
            ("gpu_temperature", gpu["id"]): gpu["temperature_c"] for gpu in sample["gpus"]
            if gpu["temperature_c"] is not None and gpu["temperature_c"] > self.alerts.gpu_temperature_c
        })
        self.hot_since = {key: self.hot_since.get(key, now) for key in hot}
        for kind, title in (("cpu_temperature", "CPU 温度告警"), ("gpu_temperature", "GPU 温度告警")):
            targets = {
                key[1]: f"{key[1]} {value:.1f} °C" for key, value in hot.items()
                if key[0] == kind and now - self.hot_since[key] >= self.alerts.temperature_seconds
            }
            self.add_alert(events, kind, title, targets, timestamp, now)

        low_disks = {
            disk["mountpoint"]: f"{disk['mountpoint']} 剩余 {disk['free_bytes'] / GIB:.2f} GiB"
            for disk in sample["disks"] if disk["free_bytes"] < self.alerts.disk_free_gib * GIB
        }
        self.add_alert(events, "disk_space", "磁盘空间告警", low_disks, timestamp, now)
        sample["task"] = self.job
        sample["last_alerts"] = list(self.last_alerts.values())
        return events

    @staticmethod
    def event(kind: str, title: str, message: str, timestamp: str) -> Snapshot:
        return {"type": kind, "title": title, "message": message, "timestamp": timestamp}

    def add_alert(self, events: list[Snapshot], kind: str, title: str, targets: dict[str, str], timestamp: str, now: float):
        messages = []
        for target, message in targets.items():
            key = (kind, target)
            last = self.last_alert_times.get(key)
            if last is None or now - last >= self.alerts.cooldown_seconds:
                self.last_alert_times[key] = now
                messages.append(message)
        if messages:
            event = self.event(kind, title, "，".join(messages), timestamp)
            self.last_alerts[kind] = event
            events.append(event)


class Monitor:
    def __init__(self, settings: Settings, identity=None):
        self.settings = settings
        self.identity = identity if identity is not None else host_identity()
        self.system = MacSystemSampler() if self.identity["platform"] == "macos" else SystemSampler()
        self.rules = Rules(settings)
        self.latest: Snapshot | None = None
        self.subscribers: set[asyncio.Queue[Snapshot]] = set()
        self.sampling_mode = settings.sampling_mode
        self.sampling_changed = asyncio.Event()
        self.notifications: asyncio.Queue[Snapshot] = asyncio.Queue(maxsize=64)
        self.lhm_retry_at: float | None = None

    @property
    def sampling_interval(self) -> float:
        if self.sampling_mode == "realtime" or (self.sampling_mode == "smart" and self.subscribers):
            return REALTIME_INTERVAL
        return ECO_INTERVAL

    def set_sampling_mode(self, mode: SamplingMode):
        if self.sampling_mode != mode:
            self.sampling_mode = mode
            self.sampling_changed.set()

    def subscribe(self) -> asyncio.Queue[Snapshot]:
        mailbox = asyncio.Queue(maxsize=1)
        if self.latest is not None:
            mailbox.put_nowait(self.latest)
        self.subscribers.add(mailbox)
        if self.sampling_mode == "smart" and len(self.subscribers) == 1:
            self.sampling_changed.set()
        return mailbox

    def unsubscribe(self, mailbox: asyncio.Queue[Snapshot]):
        self.subscribers.remove(mailbox)
        if self.sampling_mode == "smart" and not self.subscribers:
            self.sampling_changed.set()

    def publish(self, snapshot: Snapshot):
        self.latest = snapshot
        # 慢客户端只保留最新一帧，避免阻塞采集。
        for mailbox in self.subscribers:
            if mailbox.full():
                mailbox.get_nowait()
            mailbox.put_nowait(snapshot)

    async def read_lhm(self, client: httpx.AsyncClient) -> Snapshot | None:
        if self.identity["platform"] == "macos":
            return None
        if self.lhm_retry_at is not None and monotonic() < self.lhm_retry_at:
            return None

        async def fetch():
            response = await client.get(str(self.settings.lhm.url))
            response.raise_for_status()
            return parse_lhm(response.json(), self.settings.lhm.fan_mapping)

        try:
            hardware = await asyncio.wait_for(fetch(), self.settings.lhm.timeout_seconds)
        except Exception as error:
            if self.lhm_retry_at is None:
                logger.warning("LHM 未连接，暂用系统指标（%s）", type(error).__name__)
            self.lhm_retry_at = monotonic() + LHM_RETRY_SECONDS
            return None
        if self.lhm_retry_at is not None:
            logger.info("LHM 已恢复连接")
            self.lhm_retry_at = None
        return hardware

    async def run(self, client: httpx.AsyncClient, power_snapshot=None):
        while True:
            started = monotonic()
            try:
                results = await asyncio.gather(asyncio.to_thread(self.system.sample), self.read_lhm(client), return_exceptions=True)
                sample, hardware = results
                if isinstance(sample, BaseException):
                    raise sample
                if isinstance(hardware, BaseException):
                    raise hardware
                sample.update(self.identity, timestamp=utc_now(), sampling_interval_seconds=self.sampling_interval)
                temperatures = merge_hardware(sample, hardware)
                if power_snapshot is not None:
                    sample["power"] = power_snapshot()
                events = self.rules.evaluate(sample, temperatures, monotonic())
                self.publish(sample)
                if self.settings.webhook.kind != "disabled":
                    for event in events:
                        try:
                            self.notifications.put_nowait(event)
                        except asyncio.QueueFull:
                            logger.warning("通知队列已满，跳过事件 %s", event["type"])
            except Exception:
                logger.exception("本轮硬件采集失败")
            # 以本轮开始时间为基准，订阅或模式变化只重算下一轮的等待时间。
            while (delay := started + self.sampling_interval - monotonic()) > 0:
                self.sampling_changed.clear()
                try:
                    await asyncio.wait_for(self.sampling_changed.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def send_notifications(self, client: httpx.AsyncClient):
        config = self.settings.webhook
        while True:
            event = await self.notifications.get()
            try:
                await asyncio.wait_for(send_webhook(client, config, event), config.timeout_seconds)
            except Exception as error:
                # Bark URL 含设备密钥，只记录事件和异常类型。
                logger.warning("Webhook 发送失败，事件 %s（%s）", event["type"], type(error).__name__)


async def send_webhook(client: httpx.AsyncClient, config: WebhookConfig, event: Snapshot):
    if config.kind == "bark" and config.bark_url_template:
        url = config.bark_url_template.format(title=quote(event["title"], safe=""), body=quote(event["message"], safe=""))
        response = await client.get(url)
    elif config.kind == "json":
        response = await client.post(str(config.json_url), json=event)
    else:
        return
    response.raise_for_status()
    if config.kind == "bark":
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ValueError("Bark did not confirm delivery")
