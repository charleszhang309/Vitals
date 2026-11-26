"""合并 Apple 与 Android 健康数据，生成本地时区的健康概览。"""

from collections import defaultdict
import csv
from datetime import datetime, time, timedelta, timezone
from hmac import compare_digest
import json
import logging
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .config import HealthConfig, MIB, ROOT
from . import health_android as android
from .monitor import utc_now


logger = logging.getLogger(__name__)
MAX_EXPORT = 32 * MIB
TOTALS = ("step_count", "active_energy", "apple_exercise_time", "walking_running_distance")
LATEST = ("resting_heart_rate", "heart_rate_variability", "blood_oxygen_saturation", "respiratory_rate")
DETAIL_METRICS = {
    "heart": ("heart_rate", "resting_heart_rate", "walking_heart_rate_average", "heart_rate_variability"),
    "activity": ("active_energy", "basal_energy_burned", "apple_exercise_time", "apple_stand_hour", "apple_stand_time", "total_energy_burned"),
    "steps": ("step_count", "walking_running_distance", "flights_climbed", "walking_speed", "walking_step_length"),
    "sleep": ("sleep_analysis", "sleep_segment"),
}
TREND_METRICS = {"heart": ("heart_rate", {"24h": 1, "7d": 7}),
                 "oxygen": ("blood_oxygen_saturation", {"7d": 7, "30d": 30}),
                 "respiratory": ("respiratory_rate", {"7d": 7, "30d": 30})}
ALIASES = {"heart_rate_variability_sdnn": "heart_rate_variability", "active_energy_burned": "active_energy",
           "basal_energy": "basal_energy_burned"}
KNOWN = {*LATEST, *(name for group in DETAIL_METRICS.values() for name in group if name != "sleep_segment")}
STAGES = {"deep": "deep", "rem": "rem", "core": "core", "awake": "awake", "asleep": "asleep", "inbed": "in_bed"}


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def timestamp(value, zone: ZoneInfo) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().replace("Z", "+00:00").replace(" +", "+").replace(" -", "-")
    # Python 3.10 的 ISO 解析要求偏移量带冒号，导出文件则使用 ±HHMM。
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = f"{value[:-2]}:{value[-2:]}"
    try:
        parsed = datetime.fromisoformat(value)
        return iso(parsed if parsed.tzinfo else parsed.replace(tzinfo=zone))
    except (ValueError, OverflowError):
        return None


def quantity(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) and 0 <= number <= 1e12 else None


def scalar(name: str, value: float, unit: str) -> float:
    if name == "walking_running_distance":
        return value * 1.609344 if unit in {"mi", "mile", "miles"} else value / 1000 if unit == "m" else value
    if name in {"active_energy", "basal_energy_burned", "total_energy_burned"}:
        return value / {"kj": 4.184, "j": 4184, "cal": 1000}.get(unit, 1)
    if name in {"apple_exercise_time", "apple_stand_time"}:
        return value * {"ms": 1 / 60000, "s": 1 / 60, "sec": 1 / 60, "seconds": 1 / 60, "h": 60, "hr": 60, "hours": 60}.get(unit, 1)
    if name == "walking_speed":
        return value * {"m/s": 3.6, "m/sec": 3.6, "mph": 1.609344, "mi/h": 1.609344,
                        "mi/hr": 1.609344, "ft/s": 1.09728}.get(unit, 1)
    if name == "walking_step_length":
        return value * {"m": 100, "mm": 0.1, "in": 2.54, "ft": 30.48}.get(unit, 1)
    if name == "heart_rate_variability" and unit in {"s", "sec", "seconds"}:
        return value * 1000
    if name == "blood_oxygen_saturation" and (0 < value <= 1 or unit == "fraction"):
        return min(100, value * 100)
    return value


def normalize_sleep(stages: dict, total, in_bed, start, end, bed_start=None, bed_end=None) -> dict:
    asleep_keys = ("deep", "rem", "core", "asleep")
    measured_asleep = sum(stages.get(key) or 0 for key in asleep_keys)
    awake = stages.get("awake")
    if total is None and any(stages.get(key) is not None for key in asleep_keys):
        total = measured_asleep
    if in_bed is None and bed_start and bed_end:
        in_bed = max(0, (datetime.fromisoformat(bed_end) - datetime.fromisoformat(bed_start)).total_seconds() / 60)
        if in_bed == 0:
            in_bed = None
    adjusted = False
    if in_bed is not None and total is not None and total > max(0, in_bed - (awake or 0)):
        total = max(0, in_bed - (awake or 0))
        adjusted = True
    if in_bed is not None and awake is not None and awake > in_bed:
        stages["awake"] = in_bed
        adjusted = True
    if total is not None and measured_asleep > total:
        for key in asleep_keys:
            if stages.get(key) is not None:
                stages[key] *= total / measured_asleep
        adjusted = True
    return {"start": start, "end": end, "in_bed_start": bed_start, "in_bed_end": bed_end,
            "in_bed_minutes": in_bed, "total_minutes": total, "stages": stages, "adjusted": adjusted}


def parse_apple(metrics, zone: ZoneInfo) -> dict[tuple[str, str], dict]:
    samples = {}
    for metric in metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("data"), list):
            continue
        raw_name = str(metric.get("name", "")).strip().lower()
        name = ALIASES.get(raw_name, raw_name)
        if name not in KNOWN:
            continue
        unit = str(metric.get("units", "")).strip().lower()
        for entry in metric["data"]:
            if not isinstance(entry, dict):
                continue
            mark = timestamp(entry.get("date") or entry.get("start") or entry.get("startDate"), zone)
            if name == "sleep_analysis":
                stage = STAGES.get(str(entry.get("value", "")).casefold().replace(" ", ""))
                if stage:
                    start = timestamp(entry.get("start") or entry.get("startDate"), zone) or mark
                    end = timestamp(entry.get("end") or entry.get("endDate"), zone)
                    if start and end and start < end:
                        stages = samples.setdefault(("sleep_segment", start), {})
                        stages[stage] = max(stages.get(stage, end), end)
                    continue
                start = timestamp(entry.get("sleepStart"), zone)
                end = timestamp(entry.get("sleepEnd"), zone)
                bed_start = timestamp(entry.get("inBedStart"), zone)
                bed_end = timestamp(entry.get("inBedEnd"), zone)
                mark = mark or end or start or bed_end
                factor = 1 if unit in {"min", "minutes"} else 1 / 60 if unit in {"s", "sec", "seconds"} else 60

                def minutes(key):
                    value = quantity(entry.get(key))
                    return value * factor if value is not None else None

                stages = {key: minutes(key) for key in ("deep", "rem", "core", "awake", "asleep")}
                total, in_bed = minutes("totalSleep"), minutes("inBed")
                if mark and (total is not None or in_bed is not None or any(value is not None for value in stages.values())):
                    samples[(name, mark)] = normalize_sleep(stages, total, in_bed, start, end, bed_start, bed_end)
            elif mark and name == "heart_rate":
                avg = quantity(entry.get("Avg", entry.get("avg", entry.get("qty"))))
                low = quantity(entry.get("Min", entry.get("min")))
                high = quantity(entry.get("Max", entry.get("max")))
                if avg is not None:
                    samples[(name, mark)] = {"avg": avg, "min": min(avg, low if low is not None else avg),
                                             "max": max(avg, high if high is not None else avg), "count": quantity(entry.get("count")) or 1}
            elif mark:
                value = quantity(entry.get("qty"))
                if name == "apple_stand_hour" and value is None:
                    value = {"stood": 1, "idle": 0}.get(str(entry.get("value", "")).casefold())
                if value is not None:
                    samples[(name, mark)] = {"qty": scalar(name, value, unit)}
    return samples


ANDROID_VALUES = {
    "step_count": ("count", "steps", "totalsteps", "stepcount", "total", "sum", "步数"),
    "resting_heart_rate": ("beatsperminute", "restingheartratebpm", "bpm"),
    "heart_rate_variability": ("heartratevariabilitymillis", "rmssd", "hrvms", "millis"),
    "blood_oxygen_saturation": ("percentage", "percent", "spo2", "oxygenpercent"),
    "respiratory_rate": ("rate", "breathsperminute", "respiratoryrate"),
    "active_energy": ("energy", "activekcal", "activecalories", "activecaloriesburned", "calories"),
    "total_energy_burned": ("energy", "totalkcal", "totalcalories", "calories"),
    "basal_energy_burned": ("energy", "basalkcal", "basalcalories"),
    "walking_running_distance": ("distance", "totalkm", "distancekm", "distanceinmeters"),
    "apple_exercise_time": ("durationminutes", "totaldurationminutes", "minutes", "duration"),
    "flights_climbed": ("floors", "count", "total"),
}
UNIT_VALUES = {"inkilocalories": "kcal", "kilocalories": "kcal", "kcal": "kcal", "inkilojoules": "kj", "kilojoules": "kj", "kj": "kj",
               "injoules": "j", "joules": "j", "inmeters": "m", "meters": "m", "inkilometers": "km", "kilometers": "km",
               "inmiles": "mi", "miles": "mi", "percent": "%"}


def android_number(raw):
    if isinstance(raw, str):
        raw = raw.strip().replace("\u00a0", "").replace("\u202f", "")
        if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", raw):
            raw = raw.replace(",", "")
        elif re.fullmatch(r"[+-]?\d+,\d+", raw):
            raw = raw.replace(",", ".")
    return quantity(raw)


def android_duration(raw, unit="min"):
    if isinstance(raw, str) and (clock := re.fullmatch(r"(\d{1,3}):([0-5]\d)(?::([0-5]\d))?", raw.strip())):
        return int(clock[1]) * 60 + int(clock[2]) + int(clock[3] or 0) / 60
    value = android_number(raw)
    return scalar("apple_exercise_time", value, unit) if value is not None else None


def android_mark(value, zone, day=None, offset=None):
    text = android.parse_time(value, day)
    if text and isinstance(offset, str) and re.fullmatch(r"[+-]\d{2}:?\d{2}", offset) and not re.search(r"[+-]\d{2}:?\d{2}$", text[10:]):
        if len(text) > 10 and not text.endswith("Z"):
            text += offset
    return timestamp(text, zone)


def android_times(row, zone):
    pick = android.first
    source_day = android.parse_time(pick(row, android.DAY_FIELDS))
    start_day = android.parse_time(pick(row, ("startdate", "开始日期"))) or source_day
    end_day = android.parse_time(pick(row, ("enddate", "结束日期"))) or source_day
    start = android_mark(pick(row, android.START_FIELDS), zone, start_day, row.get("startzoneoffset"))
    end = android_mark(pick(row, android.END_FIELDS), zone, end_day, row.get("endzoneoffset"))
    point = android_mark(pick(row, android.TIME_FIELDS), zone, source_day, row.get("zoneoffset"))
    return start, end, point or start or end or android_mark(source_day, zone)


def add_android_sleep(samples, row, zone, start, end, mark):
    def segment(stage, first, last):
        if stage and first and last and first < last:
            existing = samples.setdefault(("sleep_segment", first), {})
            existing[stage] = max(existing.get(stage, last), last)
            return True
        return False

    pick = android.first
    day = android.parse_time(pick(row, android.DAY_FIELDS))
    explicit = pick(row, ("stage", "sleepstage", "stagetype", "睡眠阶段"))
    if explicit is not None:
        segment(android.stage_name(explicit), start, end)
        return
    phases = pick(row, ("stages", "sleepstages", "segments"))
    if isinstance(phases, list):
        found = False
        for part in phases:
            if isinstance(part, dict):
                details = android.fields(part)
                if day:
                    details.setdefault("date", day)
                first, last, _ = android_times(details, zone)
                if first and last and (start is None or start <= first) and (end is None or last <= end):
                    label = pick(details, ("stage", "stagetype", "sleepstage", "type", "value"))
                    found = segment(android.stage_name(label), first, last) or found
        if found:
            return
    stages = {}
    phase_values = android.fields(phases) if isinstance(phases, dict) else row
    for label, value in phase_values.items():
        for suffix, unit in (("minutes", "min"), ("min", "min"), ("seconds", "s"), ("hours", "h"), ("", "min")):
            phase = label.removesuffix(suffix).removesuffix("duration")
            if phase_values is row and phase == "sleep":
                break
            if label.endswith(suffix) and (stage := android.stage_name(phase)):
                if (minutes := android_duration(value, unit)) is not None:
                    stages[stage] = minutes
                break
    explicit_minutes = pick(row, ("totalsleepminutes", "totalsleepdurationminutes", "sleepdurationminutes", "totaldurationminutes", "totalminutes", "durationminutes"))
    total = android_duration(explicit_minutes)
    if total is None:
        raw = pick(row, ("totalsleep", "sleepduration", "duration", "value", "total"))
        total = android_duration(raw, str(pick(row, ("unit", "units")) or "min").lower())
    if start and end and start < end:
        length = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
        if total is None and not stages and not phases:
            segment("asleep", start, end)
            return
    else:
        length = None
    if (end or mark) and (total is not None or stages):
        in_bed = stages.pop("in_bed", None)
        classified = sum(stages.get(stage, 0) for stage in ("deep", "rem", "core", "asleep"))
        if total is not None and total > classified:
            stages["asleep"] = stages.get("asleep", 0) + total - classified
        samples[("sleep_analysis", end or mark)] = normalize_sleep(stages, total, in_bed if in_bed is not None else length,
                                                                  start, end, start, end)


def parse_android(records, zone: ZoneInfo):
    samples = {}
    pick = android.first
    for name, row in records:
        start, end, mark = android_times(row, zone)
        if name == "sleep_analysis":
            add_android_sleep(samples, row, zone, start, end, mark)
        elif name == "heart_rate":
            readings = pick(row, ("samples", "readings"))
            points = []
            if isinstance(readings, list):
                for item in readings:
                    if isinstance(item, dict):
                        record = android.fields(item)
                        _, _, at = android_times(record, zone)
                        bpm = android_number(pick(record, ("beatsperminute", "bpm", "heartrate", "value", "qty")))
                        if at and bpm is not None:
                            points.append((at, bpm))
            if points:
                samples.update(((name, at), {"avg": bpm, "min": bpm, "max": bpm, "count": 1}) for at, bpm in points)
                continue
            avg = android_number(pick(row, ("avg", "average", "avgbpm", "averagebpm", "averageheartrate", "beatsperminute", "bpm", "heartrate", "value", "qty")))
            low = android_number(pick(row, ("min", "minbpm", "minimumheartrate")))
            high = android_number(pick(row, ("max", "maxbpm", "maximumheartrate")))
            if mark and avg is not None:
                samples[(name, mark)] = {"avg": avg, "min": min(avg, low if low is not None else avg),
                                         "max": max(avg, high if high is not None else avg),
                                         "count": android_number(pick(row, ("recordscount", "samplecount", "count"))) or 1}
        elif mark:
            raw = pick(row, ("value", "qty", *ANDROID_VALUES.get(name, ())))
            unit = str(pick(row, ("unit", "units")) or "").lower()
            if isinstance(raw, dict):
                measures = android.fields(raw)
                field = next((field for field in UNIT_VALUES if field in measures), None)
                raw, unit = (measures[field], UNIT_VALUES[field]) if field else (pick(measures, ("value", "qty")), str(measures.get("unit", unit)).lower())
            if "distanceinmeters" in row and not unit:
                unit = "m"
            if isinstance(raw, str) and (entry := re.fullmatch(r"([\d.,]+)\s+([a-zA-Z/%]+)", raw.strip())):
                raw, unit = entry[1], entry[2].lower()
            value = android_number(raw)
            if name == "apple_exercise_time" and value is None and start and end and start < end:
                value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
                unit = "min"
            if value is not None:
                normalized = android.UNITS.get(android.key(unit), unit)
                samples[(name, mark)] = {"qty": scalar(name, value, normalized)}
    return samples


def parse_export(document, zone: ZoneInfo, hint=None) -> dict[tuple[str, str], dict]:
    data = document.get("data") if isinstance(document, dict) else None
    metrics = data.get("metrics") if isinstance(data, dict) else None
    samples = {}
    apple_envelope = isinstance(metrics, list)
    if isinstance(metrics, list):
        remaining = []
        for item in metrics:
            shaped = isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("data"), list)
            apple_envelope = apple_envelope and shaped
            if shaped and (parsed := parse_apple([item], zone)):
                samples.update(parsed)
            else:
                remaining.append(item)
        document = remaining
    samples.update(parse_android(android.records(document, hint), zone))
    if samples or apple_envelope or document == {"metrics": []} or document == []:
        return samples
    raise ValueError("无法识别健康 JSON 或有效测量值")


def sleep_segments(rows):
    return ((row["at"], end, stage) for row in rows for stage, end in json.loads(row["payload"]).items())


def sleep_sessions(rows):
    segments = sorted(sleep_segments(rows), key=lambda part: part[1], reverse=True)
    session = []
    start = None
    for segment in segments:
        beginning, end = map(datetime.fromisoformat, segment[:2])
        if start is not None and start - end > timedelta(hours=2):
            yield measure_sleep(session)
            session = []
            start = None
        session.append(segment)
        start = beginning if start is None else min(start, beginning)
    if session:
        yield measure_sleep(session)


def sleep_intervals(segments):
    changes = defaultdict(list)
    for beginning, end, stage in segments:
        changes[beginning].append((stage, 1))
        changes[end].append((stage, -1))
    phases = ("awake", "deep", "rem", "core", "asleep")
    active = defaultdict(int)
    previous = None
    for instant in sorted(changes):
        if previous is not None and any(active.values()):
            stage = next((phase for phase in phases if active[phase]), None)
            yield previous, instant, stage
        for stage, delta in changes[instant]:
            active[stage] += delta
        previous = instant


def measure_sleep(segments):
    phases = ("awake", "deep", "rem", "core", "asleep")
    measured = defaultdict(float)
    first_asleep = last_asleep = None
    for beginning, end, stage in sleep_intervals(segments):
        duration = (datetime.fromisoformat(end) - datetime.fromisoformat(beginning)).total_seconds() / 60
        measured["in_bed"] += duration
        if stage:
            measured[stage] += duration
            if stage != "awake":
                first_asleep = first_asleep or beginning
                last_asleep = end
    return normalize_sleep({key: measured.get(key) for key in phases}, None, measured["in_bed"],
                           first_asleep, last_asleep, min(part[0] for part in segments), max(part[1] for part in segments))


def hourly_sleep(rows, start, end):
    hours = defaultdict(float)
    hour = timedelta(hours=1)
    for beginning, finish, stage in sleep_intervals(sleep_segments(rows)):
        if stage is None:
            continue
        first = max(datetime.fromisoformat(beginning), start)
        last = min(datetime.fromisoformat(finish), end)
        while first < last:
            bucket = start + ((first - start) // hour) * hour
            boundary = min(bucket + hour, last)
            hours[bucket] += (boundary - first).total_seconds() / 60 if stage != "awake" else 0
            first = boundary
    return [{"time": iso(at), "sleep_minutes": value} for at, value in sorted(hours.items())]


def sleep_options(summaries, segments):
    for row in summaries:
        record = json.loads(row["payload"])
        yield record["end"] or record["in_bed_end"] or row["at"], record
    for record in sleep_sessions(segments):
        yield record["end"] or record["in_bed_end"], record


def heart_summary(values):
    return {"min": min(value["min"] for value in values), "max": max(value["max"] for value in values),
            "avg": sum(value["avg"] * value["count"] for value in values) / sum(value["count"] for value in values)}


def summarize_quantity(values, operation=sum):
    return operation(values) if values else None


def mean(values):
    return sum(values) / len(values)


def sleep_records(db, first, end, zone):
    lower = iso(datetime.combine(first - timedelta(days=1), time.min, zone))
    return tuple(db.execute("""SELECT at, payload FROM health_samples
        WHERE metric = ? AND at >= ? AND at < ? ORDER BY at""", (metric, lower, end)).fetchall()
                 for metric in ("sleep_analysis", "sleep_segment"))


def sleep_by_day(records, zone, end):
    daily = {}
    for awake_at, sleep in records:
        if awake_at >= end or sleep["total_minutes"] is None:
            continue
        day = datetime.fromisoformat(awake_at).astimezone(zone).date()
        if day not in daily or sleep["total_minutes"] > daily[day]["total_minutes"]:
            daily[day] = sleep
    return daily


class HealthStore:
    def __init__(self, connect, config: HealthConfig):
        self.connect = connect
        self.zone = ZoneInfo(config.timezone)
        self.config = config
        self.export_dir = ROOT / config.export_dir if config.export_dir else None

    def initialize(self):
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS health_samples (
                metric TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL,
                priority INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(metric, at)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS health_updated ON health_samples(updated_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS health_file_imports (
                path TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL
            )""")

    def ingest(self, document, priority: int, file_signature=None) -> int:
        return self.write_samples(parse_export(document, self.zone), priority, file_signature)

    def write_samples(self, samples, priority, file_signature=None):
        with self.connect() as db:
            # 先取得 SQLite 写锁，按实际写入顺序处理直推和文件。
            db.execute("BEGIN IMMEDIATE")
            if file_signature is not None:
                path, modified_ns, size = file_signature
                existing = db.execute("SELECT modified_ns, size FROM health_file_imports WHERE path = ?", (path,)).fetchone()
                if existing is not None and (tuple(existing) == (modified_ns, size) or existing[0] > modified_ns):
                    return 0
            now = utc_now()
            cursor = db.executemany("""
                INSERT INTO health_samples(metric, at, payload, priority, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(metric, at) DO UPDATE SET
                    payload = CASE WHEN excluded.metric = 'sleep_segment'
                                   THEN json_patch(health_samples.payload, excluded.payload)
                                   ELSE excluded.payload END,
                    priority = excluded.priority, updated_at = excluded.updated_at
                WHERE (CASE WHEN excluded.metric = 'sleep_segment'
                            THEN json_patch(health_samples.payload, excluded.payload)
                            ELSE json(excluded.payload) END) <> json(health_samples.payload)
            """, ((metric, at, json.dumps(value, sort_keys=True), priority, now) for (metric, at), value in samples.items()))
            if file_signature is not None:
                db.execute("INSERT OR REPLACE INTO health_file_imports VALUES (?, ?, ?)", file_signature)
            return cursor.rowcount

    def scan(self):
        if self.export_dir is None:
            return
        root = self.export_dir.resolve()
        apple, android_files = [], []
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".json", ".csv"}:
                continue
            try:
                stat = path.stat()
                if path.is_file() and path.resolve().is_relative_to(root):
                    group = apple if path.name.casefold().startswith("healthautoexport-") else android_files
                    group.append((stat.st_mtime_ns, stat.st_size, path))
            except OSError:
                continue
        ordered = sorted(apple)[-2:] + sorted(android_files)
        remaining = 24
        for modified, size, path in ordered:
            if not remaining:
                break
            signature = (modified, size)
            if size > MAX_EXPORT:
                continue
            try:
                path_key = str(path.resolve())
                with self.connect() as db:
                    known = db.execute("SELECT modified_ns, size FROM health_file_imports WHERE path = ?", (path_key,)).fetchone()
                if known is not None and tuple(known) == signature:
                    continue
                with path.open("rb") as file:
                    content = file.read(MAX_EXPORT + 1)
                current = path.stat()
                if len(content) > MAX_EXPORT or (current.st_mtime_ns, current.st_size) != signature:
                    continue
                hint = android.filename_metric(path.stem) or android.filename_metric(path.parent.name)
                if path.suffix.lower() == ".csv":
                    samples = parse_android(android.csv_records(content, hint), self.zone)
                else:
                    samples = parse_export(json.loads(content), self.zone, hint)
                self.write_samples(samples, priority=1, file_signature=(path_key, *signature))
                remaining -= 1
            except (OSError, ValueError, RecursionError, csv.Error) as error:
                logger.info("健康导出文件暂未导入（%s）", type(error).__name__)

    def summary(self, now: datetime | None = None) -> dict:
        day = (now or datetime.now(timezone.utc)).astimezone(self.zone).date()
        start = iso(datetime.combine(day, time.min, self.zone))
        end = iso(datetime.combine(day + timedelta(days=1), time.min, self.zone))
        with self.connect() as db:
            updated = db.execute("SELECT MAX(updated_at) FROM health_samples").fetchone()[0]
            daily_rows = db.execute("""SELECT metric, at, payload FROM health_samples WHERE at >= ? AND at < ?
                AND metric IN ('step_count','active_energy','apple_exercise_time','walking_running_distance','heart_rate') ORDER BY at""", (start, end)).fetchall()
            latest = {}
            for name in (*LATEST, "heart_rate"):
                row = db.execute("SELECT payload FROM health_samples WHERE metric = ? ORDER BY at DESC LIMIT 1", (name,)).fetchone()
                latest[name] = json.loads(row[0]) if row else None
            sleep_rows = db.execute("SELECT at, payload FROM health_samples WHERE metric = 'sleep_analysis' ORDER BY at DESC LIMIT 7").fetchall()
            segments = db.execute("SELECT at, payload FROM health_samples WHERE metric = 'sleep_segment' ORDER BY at DESC LIMIT 512").fetchall()

        totals, beats, hours = defaultdict(list), [], defaultdict(list)
        for row in daily_rows:
            values = json.loads(row["payload"])
            if row["metric"] == "heart_rate":
                beats.append(values)
                recorded = datetime.fromisoformat(row["at"]).astimezone(self.zone).replace(minute=0, second=0, microsecond=0)
                hours[iso(recorded)].append(values)
            else:
                totals[row["metric"]].append(values["qty"])

        sleep = max(sleep_options(sleep_rows, segments), key=lambda option: option[0], default=(None, None))[1]
        return {
            "available": updated is not None, "updated_at": updated, "date": day.isoformat(), "timezone": self.config.timezone,
            "day_start": start, "day_end": end, "totals": {name: sum(totals[name]) if totals[name] else None for name in TOTALS},
            "latest": {name: latest[name]["qty"] if latest[name] else None for name in LATEST},
            "heart_rate": {"current": latest["heart_rate"]["avg"] if latest["heart_rate"] else None,
                           "today": heart_summary(beats) if beats else None,
                           "hourly": [{"time": at, **heart_summary(values)} for at, values in sorted(hours.items())]},
            "sleep": sleep,
        }

    def weekly_comparison(self, now: datetime | None = None) -> dict:
        today = (now or datetime.now(timezone.utc)).astimezone(self.zone).date()
        dates = [today - timedelta(days=offset) for offset in range(14, 0, -1)]
        start, end = (iso(datetime.combine(day, time.min, self.zone)) for day in (dates[0], today))
        steps, resting = defaultdict(list), defaultdict(list)
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute("""SELECT metric, at, payload FROM health_samples
                WHERE metric IN ('step_count', 'resting_heart_rate') AND at >= ? AND at < ?""", (start, end))
            for row in rows:
                day = datetime.fromisoformat(row["at"]).astimezone(self.zone).date()
                records = steps if row["metric"] == "step_count" else resting
                records[day].append(json.loads(row["payload"])["qty"])
            summaries, segments = sleep_records(db, dates[0], end, self.zone)
        sleeps = sleep_by_day(sleep_options(summaries, segments), self.zone, end)
        daily = {
            "steps": {day: sum(values) for day, values in steps.items()},
            "sleep_minutes": {day: sleep["total_minutes"] for day, sleep in sleeps.items()},
            "resting_heart_rate": {day: mean(values) for day, values in resting.items()},
        }
        metrics = {}
        for name, values in daily.items():
            previous = [values[day] for day in dates[:7] if day in values]
            recent = [values[day] for day in dates[7:] if day in values]
            earlier_avg = mean(previous) if previous else None
            recent_avg = mean(recent) if recent else None
            delta = recent_avg - earlier_avg if recent and previous else None
            metrics[name] = {"recent": recent_avg, "previous": earlier_avg, "recent_days": len(recent),
                             "previous_days": len(previous), "delta": delta}
            if name == "steps":
                metrics[name]["percent_change"] = delta / earlier_avg * 100 if delta is not None and earlier_avg else None
        return {"recent": {"start": dates[7].isoformat(), "end": dates[-1].isoformat()},
                "previous": {"start": dates[0].isoformat(), "end": dates[6].isoformat()}, "metrics": metrics}

    def last_night_minutes(self, now: datetime) -> float | None:
        day = now.astimezone(self.zone).date()
        midnight = iso(datetime.combine(day, time.min, self.zone))
        end = iso(now)
        with self.connect() as db:
            db.execute("BEGIN")
            summaries, segments = sleep_records(db, day, end, self.zone)
        candidates = [(wake, record) for wake, record in sleep_options(summaries, segments)
                      if midnight <= wake < end and record["total_minutes"] is not None]
        # 有跨午夜的记录时优先使用它们，避免同一天的短午睡盖过昨晚。
        overnight = [(wake, record) for wake, record in candidates
                     if (record["in_bed_start"] or record["start"] or wake) < midnight]
        best = sleep_by_day(overnight or candidates, self.zone, end).get(day)
        return best["total_minutes"] if best else None

    def trend(self, metric: str, period: str, now: datetime | None = None) -> dict:
        name, periods = TREND_METRICS[metric]
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        today = current.astimezone(self.zone).date()
        hourly = period == "24h"
        dates = [today - timedelta(days=offset) for offset in range(periods[period] - 1, -1, -1)]
        start = current - timedelta(hours=24) if hourly else datetime.combine(dates[0], time.min, self.zone)
        start_at, end_at = iso(start), iso(current)
        buckets = defaultdict(list)
        all_values = []
        with self.connect() as db:
            rows = db.execute("""SELECT at, payload FROM health_samples
                WHERE metric = ? AND at >= ? AND at <= ? ORDER BY at""", (name, start_at, end_at))
            for row in rows:
                local = datetime.fromisoformat(row["at"]).astimezone(self.zone)
                if hourly:
                    rounded = local.replace(minute=0, second=0, microsecond=0)
                    # 起点通常落在整点之后，首个不完整时段仍归入图表范围。
                    bucket = max(start_at, iso(rounded))
                else:
                    bucket = local.date()
                payload = json.loads(row["payload"])
                value = payload if metric == "heart" else payload["qty"]
                buckets[bucket].append(value)
                all_values.append(value)
        if metric == "heart":
            stats = heart_summary(all_values) if all_values else {"min": None, "avg": None, "max": None}
            if hourly:
                points = [{"time": bucket, **heart_summary(samples)} for bucket, samples in buckets.items()]
            else:
                points = [{"date": day.isoformat(), **(heart_summary(buckets[day]) if buckets[day]
                           else {"min": None, "avg": None, "max": None})} for day in dates]
        else:
            stats = {"latest": all_values[-1] if all_values else None,
                     "avg": summarize_quantity(all_values, mean)}
            points = [{"date": day.isoformat(), "avg": summarize_quantity(buckets[day], mean)} for day in dates]
        return {"metric": metric, "period": period, "timezone": self.config.timezone,
                "start": start_at, "end": end_at, "stats": stats, "points": points}

    def detail(self, category: str, now: datetime | None = None) -> dict:
        today = (now or datetime.now(timezone.utc)).astimezone(self.zone).date()
        dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        start = iso(datetime.combine(today, time.min, self.zone))
        end = iso(datetime.combine(today + timedelta(days=1), time.min, self.zone))
        week_start = iso(datetime.combine(dates[0], time.min, self.zone))
        result = {"category": category, "timezone": self.config.timezone, "day_start": start, "day_end": end}
        with self.connect() as db:
            # 明细里的当天、周趋势和最新指标来自同一个读快照。
            db.execute("BEGIN")
            if category == "sleep":
                return {**result, **self.sleep_detail(db, dates, week_start, end)}
            names = DETAIL_METRICS[category]
            slots = ",".join("?" for _ in names)
            rows = db.execute(f"""SELECT metric, at, payload FROM health_samples
                WHERE metric IN ({slots}) AND at >= ? AND at < ? ORDER BY at""", (*names, week_start, end))
            daily = defaultdict(lambda: defaultdict(list))
            hourly = defaultdict(list)
            hrv = []
            for row in rows:
                at = datetime.fromisoformat(row["at"]).astimezone(self.zone)
                payload = json.loads(row["payload"])
                name = row["metric"]
                if name == "heart_rate":
                    if at.date() == today:
                        hourly[iso(at.replace(minute=0, second=0, microsecond=0))].append(payload)
                    continue
                value = payload["qty"]
                daily[at.date()][name].append(value)
                if name == names[0] and at.date() == today:
                    hourly[iso(at.replace(minute=0, second=0, microsecond=0))].append(value)
                if name == "heart_rate_variability" and at.date() == today:
                    hrv.append({"time": row["at"], "value": value})
            if category == "heart":
                latest = {}
                for name in names[1:]:
                    row = db.execute("SELECT payload FROM health_samples WHERE metric = ? AND at < ? ORDER BY at DESC LIMIT 1",
                                     (name, end)).fetchone()
                    latest[name] = json.loads(row[0])["qty"] if row else None
                return {**result, "values": latest, "hrv": hrv,
                        "hourly": [{"time": at, **heart_summary(values)} for at, values in hourly.items()],
                        "weekly": [{"date": day.isoformat(), "resting_heart_rate":
                                    summarize_quantity(daily[day]["resting_heart_rate"], mean)} for day in dates]}
            averages = {"walking_speed", "walking_step_length"}
            return {**result,
                    "values": {name: summarize_quantity(daily[today][name], mean if name in averages else sum) for name in names},
                    "hourly": [{"time": at, names[0]: sum(values)} for at, values in hourly.items()],
                    "weekly": [{"date": day.isoformat(), **{name: summarize_quantity(daily[day][name])
                                for name in names[:2] if category == "activity" or name == names[0]}} for day in dates]}

    def sleep_detail(self, db, dates, start, end):
        # 睡眠通常跨午夜，额外读取前一天的分段，以醒来日归档。
        lower = iso(datetime.fromisoformat(start) - timedelta(days=1))
        summaries = db.execute("""SELECT at, payload FROM health_samples
            WHERE metric = 'sleep_analysis' AND at >= ? AND at < ?""", (lower, end)).fetchall()
        segments = db.execute("""SELECT at, payload FROM health_samples
            WHERE metric = 'sleep_segment' AND at >= ? AND at < ?""", (lower, end)).fetchall()
        records = list(sleep_options(summaries, segments))
        if not records:
            summaries = db.execute("""SELECT at, payload FROM health_samples WHERE metric = 'sleep_analysis'
                AND at < ? ORDER BY at DESC LIMIT 7""", (end,)).fetchall()
            segments = db.execute("""SELECT at, payload FROM health_samples WHERE metric = 'sleep_segment'
                AND at < ? ORDER BY at DESC LIMIT 512""", (end,)).fetchall()
            records = list(sleep_options(summaries, segments))
        by_day = sleep_by_day(records, self.zone, end)
        latest = None
        for awake_at, sleep in sorted(records, key=lambda option: option[0]):
            if awake_at >= end or sleep["total_minutes"] is None:
                continue
            latest = sleep
        night = {"blood_oxygen_saturation": None, "respiratory_rate": None}
        if latest:
            minutes = latest["in_bed_minutes"]
            scale = minutes or latest["total_minutes"] + (latest["stages"].get("awake") or 0)
            latest["efficiency_percent"] = latest["total_minutes"] / minutes * 100 if minutes else None
            latest["stage_percent"] = {key: value / scale * 100 if scale and value is not None else None
                                       for key, value in latest["stages"].items()}
            first = latest["start"] or latest["in_bed_start"]
            last = latest["end"] or latest["in_bed_end"]
            if first and last and first < last:
                for name in night:
                    rows = db.execute("SELECT payload FROM health_samples WHERE metric = ? AND at >= ? AND at <= ?",
                                      (name, first, last))
                    night[name] = summarize_quantity([json.loads(row[0])["qty"] for row in rows], mean)
        day_start = datetime.combine(dates[-1], time.min, self.zone).astimezone(timezone.utc)
        return {"sleep": latest, "values": night, "hourly": hourly_sleep(segments, day_start, datetime.fromisoformat(end)),
                "weekly": [{"date": day.isoformat(),
                "total_minutes": by_day.get(day, {}).get("total_minutes")} for day in dates]}


def health_router(health: HealthStore) -> APIRouter:
    router = APIRouter(prefix="/api/health", tags=["health"])

    @router.get("/summary")
    def summary():
        return health.summary()

    @router.get("/weekly-comparison")
    def weekly_comparison():
        return health.weekly_comparison()

    @router.get("/detail/{category}")
    def detail(category: str):
        if category not in DETAIL_METRICS:
            raise HTTPException(404, "未知的健康类别")
        return health.detail(category)

    @router.get("/trend/{metric}")
    def trend(metric: str, period: str):
        if metric not in TREND_METRICS:
            raise HTTPException(404, "未知的健康趋势指标")
        if period not in TREND_METRICS[metric][1]:
            raise HTTPException(422, "该指标不支持此时间范围")
        return health.trend(metric, period)

    @router.post("/ingest", openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"oneOf": [{"type": "object"}, {"type": "array", "items": {"type": "object"}}],
                   "example": {"metrics": [{"type": "steps", "value": 8500, "start": "2026-11-25T08:00:00Z"}]}},
    }}}})
    async def ingest(request: Request):
        token = health.config.ingest_token
        if token and token.get_secret_value() and not compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {token.get_secret_value()}".encode(),
        ):
            raise HTTPException(401, "健康导入令牌不匹配", headers={"WWW-Authenticate": "Bearer"})
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_EXPORT:
                raise HTTPException(413, "健康导入上限为 32 MiB，请在 App 中分批发送")
            data.extend(chunk)

        def decode():
            return health.ingest(json.loads(data), priority=2)

        try:
            return {"imported": await run_in_threadpool(decode)}
        except (ValueError, RecursionError) as error:
            raise HTTPException(422, "需要可识别的 Health Auto Export、Health Connect 或 Tasker 健康 JSON") from error

    return router
