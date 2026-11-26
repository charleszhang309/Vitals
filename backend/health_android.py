"""Android 导出格式的字段识别。持久化和时间轴由 health.py 共用。"""

import csv
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import io
import re
import unicodedata


def key(value):
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if char.isalnum())


NAMES = {
    "step_count": ("steps", "step count", "total steps", "steps count", "steps (count)", "步数", "总步数"),
    "heart_rate": ("heart rate", "heart rate (bpm)", "heart rate (beats/min)", "bpm", "average heart rate", "avg bpm", "avg heart rate bpm", "心率", "平均心率"),
    "resting_heart_rate": ("resting heart rate", "resting heart rate bpm", "静息心率"),
    "heart_rate_variability": ("heart rate variability", "heart rate variability rmssd", "hrv", "hrv ms"),
    "blood_oxygen_saturation": ("oxygen saturation", "oxygen saturation percent", "blood oxygen saturation", "spo2", "血氧"),
    "respiratory_rate": ("respiratory rate", "respiratory rate per minute", "呼吸频率"),
    "sleep_analysis": ("sleep", "sleep session", "sleep analysis", "sleep duration", "total sleep", "total sleep duration", "睡眠", "睡眠时长"),
    "active_energy": ("active calories burned", "active calories", "active energy", "active energy burned", "active energy expenditure", "活动能量", "活动消耗", "活动卡路里"),
    "total_energy_burned": ("total calories burned", "total calories", "total energy burned", "total energy", "calories burned", "calories", "总热量", "总消耗"),
    "basal_energy_burned": ("basal energy burned", "basal calories burned", "basal calories"),
    "walking_running_distance": ("distance", "walking running distance", "distance traveled", "步行跑步距离"),
    "apple_exercise_time": ("exercise session", "exercise duration", "exercise time", "active minutes", "锻炼时长"),
    "flights_climbed": ("floors climbed", "flights climbed", "楼层"),
}
METRICS = {key(alias): name for name, names in NAMES.items() for alias in (name, *names)}
UNITS = {"kcal": "kcal", "kilocalories": "kcal", "calories": "kcal", "kj": "kj", "kilojoules": "kj",
         "joules": "j", "km": "km", "m": "m", "meters": "m", "metres": "m", "mi": "mi", "miles": "mi",
         "minutes": "min", "min": "min", "seconds": "s", "sec": "s", "hours": "h", "hr": "h",
         "milliseconds": "ms", "ms": "ms", "bpm": "bpm", "count": "count", "percent": "%"}
TIME_FIELDS = ("time", "timestamp", "datetime", "recordedat", "dateandtime", "recordtime", "datetimelocal", "datetimestamp", "测量时间")
START_FIELDS = ("start", "starttime", "startdatetime", "startdatetimelocal", "startdate", "starttimestamp", "starttimeutc", "开始时间", "开始日期")
END_FIELDS = ("end", "endtime", "enddatetime", "enddatetimelocal", "enddate", "endtimestamp", "endtimeutc", "结束时间", "结束日期")
DAY_FIELDS = ("date", "day", "日期")
TYPE_FIELDS = ("type", "recordtype", "datatype", "metric", "name", "method")
ENVELOPES = ("healthconnect", "hcgateway", "healthsync", "data", "result", "results", "records", "metrics", "messages", "entries", "items", "samples", "sessions", "readings")
STAGE_NAMES = {
    "1": "awake", "2": "asleep", "4": "core", "5": "deep", "6": "rem", "7": "awake",
    "awake": "awake", "awakeinbed": "awake", "awakeinbedtime": "awake", "wake": "awake", "清醒": "awake",
    "sleeping": "asleep", "asleep": "asleep", "sleep": "asleep", "unclassified": "asleep",
    "light": "core", "lightsleep": "core", "core": "core", "浅睡": "core", "核心": "core",
    "deep": "deep", "deepsleep": "deep", "深睡": "deep", "rem": "rem", "remsleep": "rem",
    "inbed": "in_bed", "durationinbed": "in_bed", "在床": "in_bed",
}


def fields(row):
    return {key(name): value for name, value in row.items()}


def first(row, names):
    return next((row[name] for name in names if name in row and row[name] is not None and row[name] != ""), None)


def metric_name(value):
    return METRICS.get(key(value).removesuffix("record"))


def metric_column(label):
    normalized = key(label)
    if name := metric_name(normalized):
        return name, "bpm" if normalized.endswith("bpm") else ""
    for suffix in sorted(UNITS, key=len, reverse=True):
        if normalized.endswith(suffix) and (name := metric_name(normalized[:-len(suffix)])):
            return name, UNITS[suffix]
    return None, ""


def stage_name(raw):
    return STAGE_NAMES.get(key(raw).removeprefix("sleepsessionrecordstage").removeprefix("stagetype"))


def parse_time(value, fallback_day=None):
    if isinstance(value, dict):
        seconds = first(fields(value), ("epochsecond", "seconds"))
        if isinstance(seconds, (int, float)):
            try:
                return datetime.fromtimestamp(seconds + (value.get("nano") or value.get("nanoseconds") or 0) / 1e9, timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError, TypeError):
                return None
        value = first(fields(value), ("timestamp", "datetime", "time", "date"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if abs(value) > 1e11:
                value /= 1000
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"-?\d{10,13}", value):
        return parse_time(int(value))
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[aApP][mM])?", value):
        if not fallback_day:
            return None
        try:
            value = value.upper()
            clock = (datetime.strptime(value, "%I:%M %p").time() if value.endswith(("AM", "PM")) else
                     datetime.fromisoformat("2000-01-01T" + value.split(":", 1)[0].zfill(2) + ":" + value.split(":", 1)[1]).time())
            return datetime.combine(date.fromisoformat(fallback_day[:10]), clock).isoformat()
        except ValueError:
            return None
    if match := re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", value):
        return f"{match[1]}-{match[2].zfill(2)}-{match[3].zfill(2)}" + value[match.end():]
    if re.match(r"\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
        return value
    for layout in ("%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%d.%m.%Y", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, layout).isoformat()
        except ValueError:
            continue
    if re.match(r"[A-Za-z]{3},", value):
        try:
            return parsedate_to_datetime(value).isoformat()
        except (ValueError, TypeError, OverflowError):
            return None
    return value


def filename_metric(stem):
    candidate = re.sub(r"\d+", "", stem)
    candidate = key(candidate)
    for suffix in ("rollingdays", "healthsync", "daily", "weekly", "monthly", "yearly", "export", "healthconnect", "history"):
        candidate = candidate.replace(suffix, "")
    return metric_name(candidate)


def guess_metric(row):
    if first(row, ("totalsteps", "count")) is not None and not first(row, ("avg", "avgbpm", "bpm", "beatsperminute", "samplecount")):
        return "step_count"
    samples = row.get("samples")
    if "beatsperminute" in row or "bpm" in row or (isinstance(samples, list) and any(
        isinstance(sample, dict) and {"beatsperminute", "bpm"} & fields(sample).keys() for sample in samples)):
        return "heart_rate"
    if "stages" in row or "sleepstages" in row:
        return "sleep_analysis"
    return None


def records(document, inherited=None, fallback_day=None, depth=0):
    """展开 HCGateway 的 data 包装，以及常见的 records/metrics 分组。"""
    if depth > 9:
        return
    if isinstance(document, list):
        for item in document:
            yield from records(item, inherited, fallback_day, depth + 1)
        return
    if not isinstance(document, dict):
        if inherited and document is not None:
            yield inherited, {"value": document, "date": fallback_day}
        return
    row = fields(document)
    data = row.get("data")
    nested = fields(data) if isinstance(data, dict) else {}
    day = parse_time(first(row, DAY_FIELDS)) or fallback_day
    label = first(row, TYPE_FIELDS)
    name = metric_name(label) or inherited
    if isinstance(data, dict) and (name or any(item in row for item in (*TIME_FIELDS, *START_FIELDS, *DAY_FIELDS))):
        row = {**nested, **row}
        row.pop("data", None)
    name = name or (guess_metric(row) if label is None else None)
    if name:
        for group in ("records", "entries", "items", "results", "measurements", "sessions", "data"):
            if isinstance(row.get(group), (list, dict)):
                yield from records(row[group], name, day, depth + 1)
                return
        if day and all(field not in row for field in DAY_FIELDS):
            row["date"] = day
        yield name, row
        return
    for field, value in row.items():
        if field in ENVELOPES:
            yield from records(value, None, day, depth + 1)
        elif (child_name := metric_name(field)) and isinstance(value, (dict, list)):
            yield from records(value, child_name, day, depth + 1)
        elif (child_name := metric_name(field)) and value is not None and day:
            yield child_name, {"date": day, "value": value}


def csv_records(content, hint):
    codec = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    decoded = content.decode(codec)
    sample = decoded[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = max(",;\t", key=sample.split("\n", 1)[0].count)
    reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=delimiter)
    for raw in reader:
        row = fields({name: value.strip() if value else value for name, value in raw.items() if name is not None})
        if name := metric_name(first(row, TYPE_FIELDS)):
            yield name, row
            continue
        measures = [(name, unit, value) for header, value in raw.items() if header is not None and value and value.strip()
                    for name, unit in [metric_column(header)] if name]
        for name, unit, value in measures:
            if name == "sleep_analysis" and (first(row, ("stage", "sleepstage")) is not None or not value[0].isdigit()):
                continue
            yield name, {**row, "value": value.strip(), "unit": unit or first(row, ("unit", "units")) or ""}
        kinds = {name for name, _, _ in measures}
        if "sleep_analysis" not in kinds and (hint == "sleep_analysis" or first(row, ("sleepstage", "deepsleep", "deepsleepminutes", "睡眠阶段")) is not None):
            yield "sleep_analysis", row
        elif hint and hint not in kinds:
            yield hint, row
