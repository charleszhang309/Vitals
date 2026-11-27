from datetime import date
import json
import os
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.ai_log_usage import LogUsage


UTC = ZoneInfo("UTC")
JAN_02 = date(2026, 1, 2)
JAN_03 = date(2026, 1, 3)


def codex(timestamp, total=None, last=None):
    info = {}
    if total is not None:
        info["total_token_usage"] = {"total_tokens": total}
    if last is not None:
        info["last_token_usage"] = last if isinstance(last, dict) else {"total_tokens": last}
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": info}}


def claude(timestamp, message_id, **usage):
    return {"timestamp": timestamp, "type": "assistant", "message": {"id": message_id, "usage": usage}}


def line(record):
    return json.dumps(record).encode() + b"\n"


def write(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(map(line, records)))


def test_codex_counter_advances_and_buckets_in_local_time(tmp_path):
    write(tmp_path / "2026" / "01" / "rollout.jsonl",
          codex("2026-01-02T12:00:00Z", 100, 100),
          codex("2026-01-03T03:50:00Z", 150, 50),
          codex("2026-01-03T04:00:00Z", 150, 50),
          codex("2026-01-03T05:00:00Z", 150, 50),
          codex("2026-01-03T05:30:00Z", 100, 100),
          codex("2026-01-03T06:00:00Z", 180, 30))
    usage = LogUsage(codex_root=tmp_path)
    zone = ZoneInfo("America/New_York")

    assert usage.daily("codex", JAN_02, JAN_03, zone) == {JAN_02: 150, JAN_03: 30}
    assert usage.daily("codex", JAN_03, JAN_03, zone) == {JAN_03: 30}


def test_codex_last_only_usage_does_not_count_twice_when_total_returns(tmp_path):
    write(tmp_path / "rollout.jsonl",
          codex("2026-01-02T10:00:00Z", 120, 20),
          codex("2026-01-02T10:10:00Z", last={"input_tokens": 25, "cached_input_tokens": 20, "output_tokens": 5}),
          codex("2026-01-02T10:10:00Z", last={"input_tokens": 25, "cached_input_tokens": 20, "output_tokens": 5}),
          codex("2026-01-03T10:00:00Z", 160))

    assert LogUsage(codex_root=tmp_path).daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 50, JAN_03: 10}


def test_claude_counts_cache_tokens_and_deduplicates_streamed_and_copied_records(tmp_path):
    start = claude("2026-01-02T23:59:58Z", "msg_example", input_tokens=4, output_tokens=1,
                   cache_read_input_tokens=11, cache_creation_input_tokens=7,
                   cache_creation={"ephemeral_5m_input_tokens": 7})
    end = claude("2026-01-03T00:00:10Z", "msg_example", input_tokens=4, output_tokens=5,
                 cache_read_input_tokens=11, cache_creation_input_tokens=7)
    write(tmp_path / "main" / "session.jsonl", start, end)
    write(tmp_path / "fork" / "agent" / "session.jsonl", end,
          claude("2026-01-03T01:00:00Z", "msg_other", input_tokens=10, output_tokens=2,
                 cache_creation={"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 5}),
          {**end, "type": "user"})
    usage = LogUsage(claude_root=tmp_path)

    assert usage.daily("claude", JAN_02, JAN_03, UTC) == {JAN_02: 27, JAN_03: 20}
    assert usage.daily("claude", JAN_03, JAN_03, UTC) == {JAN_03: 20}


def test_cache_decodes_only_new_lines_and_recovers_partial_write(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write(path, codex("2026-01-02T10:00:00Z", 25))
    created = path.stat().st_mtime_ns
    os.utime(path, ns=(created, created - 2_000_000_000))
    usage = LogUsage(codex_root=tmp_path)
    assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 25}
    with patch.object(Path, "open", side_effect=AssertionError("unchanged log reopened")):
        assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 25}

    row = line(codex("2026-01-03T10:00:00Z", 80))
    with path.open("ab") as log:
        log.write(row[: len(row) // 2])
    assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 25}

    with path.open("ab") as log:
        log.write(row[len(row) // 2:])
    with patch("backend.ai_log_usage.json.loads", wraps=json.loads) as decode:
        assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 25, JAN_03: 55}
        assert decode.call_count == 1


def test_rewrites_and_removals_refresh_totals(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write(path, codex("2026-01-02T10:00:00Z", 25))
    usage = LogUsage(codex_root=tmp_path)
    assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 25}

    previous_mtime = path.stat().st_mtime_ns
    write(path, codex("2026-01-02T10:00:00Z", 45), codex("2026-01-03T10:00:00Z", 65))
    os.utime(path, ns=(previous_mtime, previous_mtime + 1_000_000))
    assert usage.daily("codex", JAN_02, JAN_03, UTC) == {JAN_02: 45, JAN_03: 20}

    path.unlink()
    assert usage.daily("codex", JAN_02, JAN_03, UTC) == {}


def test_unavailable_roots_and_invalid_records_are_safe(tmp_path):
    root = tmp_path / "projects"
    usage = LogUsage(claude_root=root)
    assert not usage.available("claude")
    assert usage.daily("claude", JAN_02, JAN_03, UTC) == {}

    root.mkdir()
    good = claude("2026-01-03T10:00:00Z", "msg_valid", input_tokens=8, output_tokens=5)
    (root / "session.jsonl").write_bytes(
        b'{"usage":\xff}\n'
        + line(claude("bad-date", "msg_invalid", input_tokens=99))
        + line(claude("0001-01-01T00:00:00Z", "msg_outside_zone", input_tokens=99))
        + line(claude("2026-01-03T09:00:00Z", "msg_invalid", input_tokens=True, output_tokens=-4))
        + line({"type": "assistant", "message": False})
        + line(good).rstrip(b"\n")
    )
    assert usage.available("claude")
    assert usage.daily("claude", JAN_02, JAN_03, ZoneInfo("America/New_York")) == {JAN_03: 13}
