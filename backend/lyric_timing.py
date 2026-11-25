"""将不同词库的时间轴整理成 AMLL 使用的毫秒数据。"""

from base64 import b64decode
from dataclasses import dataclass, field
import json
import re
from xml.etree import ElementTree
import zlib

import yaml


MAX_LYRIC = 96_000
MAX_TIME = 24 * 60 * 60 * 1000
STAMP = r"(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?"
LRC_STAMP = re.compile(r"\[" + STAMP + r"\]")
ANGLE_STAMP = re.compile(r"<" + STAMP + r">")
LINE_PREFIX = re.compile(r"^\s*((?:\[(?:\d{1,3}:[0-5]\d)(?:[.:]\d{1,3})?\])+)(.*)$")
LRC_OFFSET = re.compile(r"(?im)^\s*\[offset:\s*([+-]?\d{1,7})\s*\]")
TIMED_LINE = re.compile(r"^\[(\d{1,8}),(\d{1,8})\](.*)$")
WORD_MARKS = {
    "yrc": re.compile(r"\((\d{1,8}),(\d{1,8}),\d+\)"),
    "qrc": re.compile(r"\((\d{1,8}),(\d{1,8})\)"),
    "krc": re.compile(r"<(\d{1,8}),(\d{1,8}),\d+>"),
}
KRC_KEY = b"@Gaw^2tGQ61-\xce\xd2ni"


@dataclass
class LyricLine:
    start: int
    text: str
    end: int | None = None
    words: list[dict] = field(default_factory=list)
    translation: str = ""
    background: bool = False
    duet: bool = False
    roman: str = ""


def word_at(start, end, text):
    return {"startTime": start, "endTime": end, "word": text}


def stamp_time(match):
    fraction = (match[3] or "").ljust(3, "0")
    return (int(match[1]) * 60 + int(match[2])) * 1000 + int(fraction)


def parse_lrc_lines(raw):
    offset_match = LRC_OFFSET.search(raw)
    offset = int(offset_match[1]) if offset_match else 0
    lines = []
    for row in raw.splitlines():
        prefix = LINE_PREFIX.match(row)
        if not prefix:
            continue
        timestamps = [stamp_time(tag) for tag in LRC_STAMP.finditer(prefix[1])]
        tail = prefix[2].strip()
        marks = list(ANGLE_STAMP.finditer(tail))
        if not marks:
            marks = list(LRC_STAMP.finditer(tail))
        text = ANGLE_STAMP.sub("", LRC_STAMP.sub("", tail)).strip()
        for timestamp in timestamps:
            start = max(0, timestamp + offset)
            end = None
            words = []
            if len(timestamps) == 1 and marks:
                boundary = [max(start, stamp_time(mark) + offset) for mark in marks]
                if boundary == sorted(boundary):
                    cursor, beginning = 0, start
                    for mark, limit in zip(marks, boundary):
                        if segment := tail[cursor:mark.start()]:
                            words.append(word_at(beginning, limit, segment))
                        cursor, beginning = mark.end(), limit
                    if segment := tail[cursor:]:
                        words.append(word_at(beginning, beginning + 700, segment))
                    if words:
                        end = words[-1]["endTime"]
            lines.append(LyricLine(start, text, end, words))
    return sorted(lines, key=lambda line: line.start)


def parse_lrc(raw):
    return [{"time": line.start / 1000, "text": line.text} for line in parse_lrc_lines(raw)]


def parse_word_timing(raw, kind):
    marks_for = WORD_MARKS[kind]
    lines = []
    for row in raw.splitlines():
        match = TIMED_LINE.match(row.strip())
        if not match:
            continue
        start, duration, body = int(match[1]), int(match[2]), match[3]
        if start >= MAX_TIME or duration > 60_000:
            continue
        marks = list(marks_for.finditer(body))
        words = []
        for index, mark in enumerate(marks):
            if kind == "qrc":
                segment = body[marks[index - 1].end() if index else 0:mark.start()]
            else:
                segment = body[mark.end():marks[index + 1].start() if index + 1 < len(marks) else len(body)]
            time = int(mark[1]) + (start if kind == "krc" else 0)
            if segment and abs(time - start) <= 60_000:
                words.append(word_at(time, min(time + int(mark[2]), start + 60_000), segment))
        text = marks_for.sub("", body).strip()
        if text:
            lines.append(LyricLine(start, text, start + duration, words,
                                   background=text.startswith(("(", "（")) and text.endswith((")", "）"))))
    return sorted(lines, key=lambda line: line.start)


def decode_krc(encoded):
    data = b64decode(encoded, validate=True)
    if not data.startswith(b"krc1"):
        raise ValueError("not a KRC payload")
    packed = bytes(value ^ KRC_KEY[index % len(KRC_KEY)] for index, value in enumerate(data[4:]))
    stream = zlib.decompressobj()
    raw = stream.decompress(packed, MAX_LYRIC * 4 + 1)
    if not stream.eof or len(raw) > MAX_LYRIC * 4:
        raise ValueError("invalid KRC expansion")
    return raw.decode("utf-8-sig")


def krc_translations(raw, lines):
    tag = re.search(r"\[language:([A-Za-z0-9+/=]+)\]", raw)
    if not tag:
        return
    try:
        entries = json.loads(b64decode(tag[1], validate=True))["content"]
        translations = next((entry["lyricContent"] for entry in entries if entry["type"] == 1), [])
        # KRC 使用行序号。条数不符时不猜测缺行位置。
        if len(translations) == len(lines):
            for line, fields in zip(lines, translations):
                if isinstance(fields, list) and all(isinstance(value, str) for value in fields):
                    line.translation = "".join(fields).strip()
    except (TypeError, KeyError, ValueError):
        return


def xml_root(raw):
    if len(raw) > MAX_LYRIC * 2 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("unsupported lyric XML")
    return ElementTree.fromstring(raw)


def xml_attr(element, name, default=""):
    return next((value for key, value in element.attrib.items() if key.rsplit("}", 1)[-1] == name), default)


def ttml_time(value):
    if not value:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?(?:ms|s)", value):
        scale = 1 if value.endswith("ms") else 1000
        result = round(float(value[:-2] if scale == 1 else value[:-1]) * scale)
    elif re.fullmatch(r"(?:\d{1,3}:)?\d{1,3}:\d{1,2}(?:\.\d+)?", value):
        result = round(sum(float(part) * 60 ** i for i, part in enumerate(reversed(value.split(":")))) * 1000)
    elif re.fullmatch(r"\d{1,6}(?:\.\d{1,6})?", value):
        result = round(float(value) * 1000)
    else:
        return None
    return result if 0 <= result < MAX_TIME else None


def ttml_tag(node):
    return node.tag.rsplit("}", 1)[-1]


def chinese(language):
    return language.casefold().startswith(("zh", "cmn", "yue"))


def ttml_text(node, skip_background=False):
    def space(value):
        return "" if not value or not value.strip() and "\n" in value else value

    return space(node.text) + "".join(
        ("" if skip_background and xml_attr(child, "role") == "x-bg" else ttml_text(child)) + space(child.tail)
        for child in node)


def ttml_line(node, start, end, translation="", background=False, duet=False):
    words, text, echoes = [], [node.text or ""], []
    roman = ""
    for span in node:
        role = xml_attr(span, "role")
        tail = span.tail or ""
        if not tail.strip() and "\n" in tail:
            tail = ""
        if role in {"x-translation", "translation"}:
            if not translation or chinese(xml_attr(span, "lang")):
                translation = ttml_text(span).strip()
        elif role == "x-bg":
            echo, _ = ttml_line(span, ttml_time(span.get("begin")), ttml_time(span.get("end")), background=True, duet=duet)
            echoes.append(echo)
        elif role in {"x-roman", "x-romanization"}:
            roman = ttml_text(span).strip()
        else:
            content = ttml_text(span)
            text.append(content)
            first, last = ttml_time(span.get("begin")), ttml_time(span.get("end"))
            if last is None and first is not None and (duration := ttml_time(span.get("dur"))) is not None:
                last = first + duration
            if first is not None and last is not None and last >= first:
                words.append(word_at(first, last, content + tail))
        text.append(tail)
    if start is None and words:
        start = words[0]["startTime"]
    if end is None and words:
        end = words[-1]["endTime"]
    return LyricLine(start, "".join(text).strip(), end, words, translation, background, duet, roman), echoes


def parse_ttml(raw, expected_duration=None):
    root = xml_root(raw)
    body = next((node for node in root if ttml_tag(node) == "body"), None)
    if body is None:
        return []
    duration = ttml_time(body.get("dur"))
    if duration and expected_duration and abs(duration / 1000 - expected_duration) > 20:
        return []
    sidecar = {}
    for track in root.iter():
        if ttml_tag(track) != "translation":
            continue
        for node in track:
            if ttml_tag(node) != "text" or not (key := xml_attr(node, "for")):
                continue
            if key not in sidecar or chinese(xml_attr(node, "lang") or xml_attr(track, "lang")):
                sidecar[key] = (ttml_text(node, skip_background=True).strip(),
                                [ttml_text(child).strip() for child in node if xml_attr(child, "role") == "x-bg"])
    groups = []
    primary_agent = ""
    for paragraph in body.iter():
        if ttml_tag(paragraph) != "p":
            continue
        start, end = ttml_time(paragraph.get("begin")), ttml_time(paragraph.get("end"))
        duration = ttml_time(paragraph.get("dur"))
        if end is None and start is not None and duration is not None:
            end = start + duration
        translation, echo_translations = sidecar.get(xml_attr(paragraph, "key"), ("", []))
        agent = xml_attr(paragraph, "agent")
        primary_agent = primary_agent or agent
        main, echoes = ttml_line(paragraph, start, end, translation, xml_attr(paragraph, "role") == "x-bg",
                                 bool(agent and agent != primary_agent))
        if main.start is None or not main.text:
            continue
        for index, echo in enumerate(echoes):
            echo.start = main.start if echo.start is None else echo.start
            echo.end = main.end if echo.end is None else echo.end
            if not echo.translation and index < len(echo_translations):
                echo.translation = echo_translations[index]
        groups.append([main, *echoes])
    return [line for group in sorted(groups, key=lambda group: group[0].start) for line in group]


def parse_lyricsfile(raw):
    record = yaml.safe_load(raw)
    if not isinstance(record, dict) or str(record.get("version")) != "1.0" or not isinstance(record.get("lines"), list):
        return []
    lines = []
    for row in record["lines"][:1200]:
        if not isinstance(row, dict):
            continue
        text, start, end = row.get("text"), row.get("start_ms"), row.get("end_ms")
        if not isinstance(text, str) or type(start) is not int or not 0 <= start < MAX_TIME:
            continue
        end = end if type(end) is int and start <= end < MAX_TIME else None
        words = []
        segments = row.get("words")
        if isinstance(segments, list):
            for index, segment in enumerate(segments[:400]):
                if not isinstance(segment, dict):
                    continue
                word, beginning, finish = segment.get("text"), segment.get("start_ms"), segment.get("end_ms")
                if not isinstance(word, str) or type(beginning) is not int or not start <= beginning < MAX_TIME:
                    continue
                if finish is None and index + 1 < len(segments) and isinstance(segments[index + 1], dict):
                    finish = segments[index + 1].get("start_ms")
                if finish is None:
                    finish = end or beginning + 700
                if type(finish) is int and beginning <= finish < MAX_TIME:
                    words.append(word_at(beginning, finish, word))
        lines.append(LyricLine(start, text.strip(), end, words))
    return sorted(lines, key=lambda line: line.start)


def align_translations(lines, translations):
    # 优先精确匹配，再给同一来源的两个时间轴留出小幅偏差。
    for tolerance in (0, 700):
        for translation in translations:
            if not translation.text:
                continue
            options = [line for line in lines if line.text and not line.background and not line.translation
                       and abs(line.start - translation.start) <= tolerance]
            if options:
                nearest = min(options, key=lambda line: abs(line.start - translation.start))
                nearest.translation = translation.text
                translation.text = ""


def amll_timeline(lines):
    result = []
    for index, line in enumerate(lines):
        if not line.text:
            continue
        next_start = next((item.start for item in lines[index + 1:] if item.start > line.start), line.start + 4000)
        end = max(line.start + 1, line.end or min(next_start, line.start + 5000))
        words = line.words
        if (not words or "".join(word["word"] for word in words).strip() != line.text
                or any(not 0 <= word["startTime"] <= word["endTime"] < MAX_TIME for word in words)):
            words = [word_at(line.start, end, line.text)]
        result.append({"startTime": line.start, "endTime": end, "words": words, "translatedLyric": line.translation,
                       "romanLyric": line.roman, "isBG": line.background, "isDuet": line.duet})
    return result
