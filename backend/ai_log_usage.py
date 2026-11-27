"""Read aggregate token activity and Codex quota from local CLI transcripts."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from hashlib import blake2b
from itertools import chain
import json
from math import isfinite
import os
from pathlib import Path
from stat import S_ISREG
from threading import Lock
from typing import BinaryIO
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class _Usage:
    timestamp: datetime
    tokens: int | None
    cumulative: int | None = None
    message_key: tuple[str, str] | None = None
    quota: dict[str, dict[str, int | None]] | None = None


@dataclass(frozen=True)
class _FileStamp:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, stat: os.stat_result):
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass
class _FileUsage:
    stamp: _FileStamp
    offset: int
    anchors: bytes
    records: list[_Usage]
    tail: _Usage | None

    def rows(self):
        yield from self.records
        if self.tail is not None:
            yield self.tail


class LogUsage:
    """Index local transcripts in memory and return aggregate usage and quota."""

    def __init__(self, codex_root: Path | None = None, claude_root: Path | None = None):
        self._roots = {
            "codex": Path(codex_root) if codex_root is not None else Path.home() / ".codex" / "sessions",
            "claude": Path(claude_root) if claude_root is not None else Path.home() / ".claude" / "projects",
        }
        self._files: dict[str, dict[Path, _FileUsage]] = {source: {} for source in self._roots}
        self._locks = {source: Lock() for source in self._roots}

    def available(self, source: str) -> bool:
        try:
            return self._roots[source].is_dir()
        except OSError:
            return False

    def daily(self, source: str, start: date, end: date, zone: ZoneInfo) -> dict[date, int]:
        """Return tokens grouped by local date between inclusive endpoints."""
        days, _ = self._daily(source, start, end, zone)
        return days

    def codex_daily(self, start: date, end: date, zone: ZoneInfo):
        """Return Codex daily counts and the latest usable quota snapshot together."""
        return self._daily("codex", start, end, zone)

    def _daily(self, source: str, start: date, end: date, zone: ZoneInfo):
        if start > end:
            return {}, None
        with self._locks[source]:
            files = self._refresh(source, start, zone)
            latest_quota = None
            if source == "codex":
                usage = chain.from_iterable(_codex_usage(file.rows()) for file in files)
                for file in files:
                    for row in file.rows():
                        if row.quota and (latest_quota is None or row.timestamp >= latest_quota.timestamp):
                            latest_quota = row
            else:
                usage = _claude_usage(chain.from_iterable(file.rows() for file in files))
            days = defaultdict(int)
            for timestamp, tokens in usage:
                try:
                    day = timestamp.astimezone(zone).date()
                except OverflowError:
                    continue
                if start <= day <= end and tokens > 0:
                    days[day] += tokens
            return dict(days), latest_quota.quota if latest_quota else None

    def _refresh(self, source: str, start: date, zone: ZoneInfo):
        files = self._files[source]
        seen = set()
        # Live transcript modification times track writes. Allow a day for clock skew.
        recent_after = datetime.combine(start, time.min, zone).timestamp() - 86_400
        for folder, _, names in os.walk(self._roots[source]):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(folder) / name
                try:
                    stat = path.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not S_ISREG(stat.st_mode) or stat.st_mtime < recent_after:
                    continue
                seen.add(path)
                previous = files.get(path)
                if previous is not None and previous.stamp == _FileStamp.from_stat(stat):
                    continue
                try:
                    files[path] = _read_file(path, source, previous)
                except OSError:
                    continue
        for path in files.keys() - seen:
            del files[path]
        return files.values()


def _anchors(log: BinaryIO, end: int) -> bytes:
    """Identify the edges of a previously read prefix before appending to it."""
    log.seek(0)
    digest = blake2b(log.read(min(end, 2048)))
    log.seek(max(0, end - 2048))
    digest.update(log.read(min(end, 2048)))
    return digest.digest()


def _read_file(path: Path, source: str, previous: _FileUsage | None) -> _FileUsage:
    marker, extract = _PARSERS[source]
    with path.open("rb") as log:
        stamp = _FileStamp.from_stat(os.fstat(log.fileno()))
        offset = 0
        records = []
        tail = None
        if (previous is not None and stamp.size > previous.stamp.size
                and (stamp.device, stamp.inode) == (previous.stamp.device, previous.stamp.inode)
                and _anchors(log, previous.offset) == previous.anchors):
            offset = previous.offset
            records = previous.records

        log.seek(offset)
        new_records = []
        while offset < stamp.size:
            line = log.readline(stamp.size - offset)
            if not line:
                break
            record = _parse(line, extract) if marker in line else None
            if not line.endswith(b"\n"):
                # A complete final object can be used now and rechecked after a later write.
                tail = record
                break
            offset += len(line)
            if record is not None:
                new_records.append(record)
        anchors = _anchors(log, offset)
    records.extend(new_records)
    return _FileUsage(stamp, offset, anchors, records, tail)


def _object(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _parse(line: bytes, extract) -> _Usage | None:
    try:
        row = _object(json.loads(line))
        value = row.get("timestamp")
        if not isinstance(value, str):
            return None
        timestamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if timestamp.tzinfo is None:
            return None
        return extract(row, timestamp.astimezone(timezone.utc))
    except (ValueError, OverflowError, RecursionError):
        return None


def _codex_tokens(value):
    counts = _object(value)
    total = _count(counts.get("total_tokens"))
    if total is not None:
        return total
    input_tokens = _count(counts.get("input_tokens"))
    output_tokens = _count(counts.get("output_tokens"))
    if input_tokens is not None or output_tokens is not None:
        return (input_tokens or 0) + (output_tokens or 0)
    return None


def _codex_quota(value):
    quota = {}
    limits = _object(value)
    for slot in ("primary", "secondary"):
        window = _object(limits.get(slot))
        minutes = _count(window.get("window_minutes"))
        used = window.get("used_percent")
        if not minutes or type(used) not in (int, float) or (type(used) is float and not isfinite(used)):
            continue
        if minutes <= 360:
            key = "five_hour"
        elif minutes >= 1440:
            key = "seven_day"
        else:
            continue
        if key not in quota:
            quota[key] = {
                "remaining_percent": round(max(0, min(100, 100 - used))),
                "resets_at": _count(window.get("resets_at")),
            }
    return quota or None


def _codex_record(row, timestamp):
    event = _object(row.get("payload"))
    if row.get("type") != "event_msg" or event.get("type") != "token_count":
        return None
    info = _object(event.get("info"))
    total = _codex_tokens(info.get("total_token_usage"))
    last = _codex_tokens(info.get("last_token_usage"))
    quota = _codex_quota(event.get("rate_limits"))
    if total is not None or last is not None or quota:
        return _Usage(timestamp, last, cumulative=total, quota=quota)
    return None


def _codex_usage(rows):
    high_water = None
    pending = 0
    last_events = set()
    for row in rows:
        total = row.cumulative
        last = row.tokens
        event = (row.timestamp, last)
        if total is None:
            if not last or event in last_events:
                continue
            tokens = last
            pending += last
        elif high_water is None:
            # Resumed transcripts can begin with an existing session total.
            tokens = min(last, total) if last is not None else max(0, total - pending)
            high_water = total
            pending = 0
        elif total > high_water:
            advance = total - high_water
            high_water = total
            tokens = max(0, advance - pending)
            pending = max(0, pending - advance)
        else:
            # Pure rate-limit updates can repeat the last nonzero token counts.
            continue
        last_events.add(event)
        yield row.timestamp, tokens


def _claude_record(row, timestamp):
    if row.get("type") != "assistant":
        return None
    message = _object(row.get("message"))
    usage = _object(message.get("usage"))
    cache_creation = _count(usage.get("cache_creation_input_tokens"))
    if cache_creation is None:
        detail = _object(usage.get("cache_creation"))
        cache_creation = sum(_count(detail.get(key)) or 0 for key in (
            "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
        ))
    tokens = cache_creation + sum(_count(usage.get(key)) or 0 for key in (
        "input_tokens", "cache_read_input_tokens", "output_tokens",
    ))
    if tokens <= 0:
        return None
    for kind, value in (("message", message.get("id")), ("request", row.get("requestId")), ("row", row.get("uuid"))):
        if isinstance(value, str) and value:
            return _Usage(timestamp, tokens, message_key=(kind, value))
    return _Usage(timestamp, tokens)


def _claude_usage(rows):
    messages = {}
    for row in rows:
        if row.message_key is None:
            yield row.timestamp, row.tokens
            continue
        previous_time, previous_tokens = messages.get(row.message_key, (row.timestamp, 0))
        messages[row.message_key] = (min(previous_time, row.timestamp), max(previous_tokens, row.tokens))
    yield from messages.values()


_PARSERS = {
    "codex": (b'"token_count"', _codex_record),
    "claude": (b'"usage"', _claude_record),
}
