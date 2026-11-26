"""合并日历来源，隔离各来源的缓存、凭据和写入。"""

import asyncio
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import logging
import re
from time import monotonic
from typing import Annotated
from zoneinfo import ZoneInfo

from caldav.lib import error as dav_error
from fastapi import APIRouter, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .calendar_sources import CalDAVSource, ICSFeed, OAuthSource


logger = logging.getLogger(__name__)
SYNC_INTERVAL = 300
DateText = Annotated[str, StringConstraints(min_length=10, max_length=40)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    start: DateText
    end: DateText
    all_day: bool = False
    location: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=8000)


class NewEvent(EventInput):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class EventEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    start: DateText | None = None
    end: DateText | None = None
    all_day: bool | None = None
    location: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=8000)


def event_time(value, all_day):
    if all_day:
        return date.fromisoformat(value)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("missing timezone")
    return result


def event_values(payload):
    fields = payload.model_dump(exclude={"source_id"})
    try:
        start, end = (event_time(fields[key], payload.all_day) for key in ("start", "end"))
        if end <= start:
            raise ValueError("invalid interval")
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, "结束时间须晚于开始时间，非全天日程须带时区") from error
    return {**fields, "start": start, "end": end}


def event_key(value):
    return urlsafe_b64encode(value.encode()).decode().rstrip("=")


@dataclass
class SourceState:
    adapter: object
    events: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status: str = "unavailable"
    updated_at: str | None = None
    next_fetch: float = 0.0
    range_day: date | None = None

    @property
    def config(self):
        return self.adapter.config


class CalendarService:
    def __init__(self, config, zone: ZoneInfo):
        types = {"icloud": CalDAVSource, "google": OAuthSource, "outlook": OAuthSource, "ics": ICSFeed}
        self.sources = {item.id: SourceState(types[item.type](item, zone)) for item in config.sources}
        self.zone = zone

    def bounds(self, now):
        day = now.astimezone(self.zone).date()
        return (datetime.combine(day, time.min, self.zone), datetime.combine(day + timedelta(days=15), time.min, self.zone))

    def event_bounds(self, event):
        if event["all_day"]:
            return tuple(datetime.combine(event[key], time.min, self.zone) for key in ("start", "end"))
        return event["start"], event["end"]

    def public_event(self, state, event):
        return {"id": event_key(event["id"]), "source_id": state.config.id, "source_name": state.config.name,
                "color": state.config.color, "writable": state.config.can_write and event.get("writable", True),
                **{name: event[name] for name in ("title", "all_day", "location", "notes")},
                "start": event["start"].isoformat(), "end": event["end"].isoformat()}

    async def refresh_state(self, client, state, start, end):
        try:
            state.events = await state.adapter.list(client, start, end)
            state.status = "live"
            state.updated_at = datetime.now(timezone.utc).isoformat()
        except Exception as error:
            # 上游 URL、认证错误详情与日程原文保留在服务端。
            logger.info("日历同步暂不可用（%s）", type(error).__name__)
            state.status = "stale" if state.updated_at else "unavailable"
        state.range_day = start.date()
        state.next_fetch = monotonic() + (SYNC_INTERVAL if state.status == "live" else 45)

    async def summary(self, client, refresh=False, now=None):
        start, end = self.bounds(now or datetime.now(timezone.utc))

        async def ensure(state):
            if refresh or monotonic() >= state.next_fetch or state.range_day != start.date():
                async with state.lock:
                    if refresh or monotonic() >= state.next_fetch or state.range_day != start.date():
                        await self.refresh_state(client, state, start, end)

        await asyncio.gather(*(ensure(state) for state in self.sources.values()))
        contextual = []
        for state in self.sources.values():
            for event in state.events:
                first, last = self.event_bounds(event)
                # 单点事件也应在计划列表中展示。
                if first < end and max(last, first + timedelta(microseconds=1)) > start:
                    contextual.append((first, self.public_event(state, event)))
        contextual.sort(key=lambda pair: pair[0])
        return {"date": start.date().isoformat(), "timezone": self.zone.key,
                "events": [event for _, event in contextual],
                "sources": [{"id": state.config.id, "name": state.config.name, "color": state.config.color,
                             "type": state.config.type, "writable": state.config.can_write,
                             "status": state.status, "updated_at": state.updated_at} for state in self.sources.values()]}

    def agenda(self, now):
        # AI 仅消费已同步的当天日程；地址、备注、来源和凭据均不在摘要内。
        today, _ = self.bounds(now)
        tomorrow = datetime.combine(today.date() + timedelta(days=1), time.min, self.zone)
        items = []
        for state in self.sources.values():
            for event in state.events:
                start, end = self.event_bounds(event)
                if start < tomorrow and max(end, start + timedelta(microseconds=1)) > today:
                    items.append((start, {"title": re.sub(r"\s+", " ", event["title"])[:120],
                                          "start": event["start"].isoformat(), "end": event["end"].isoformat(),
                                          "all_day": event["all_day"]}))
        return [item for _, item in sorted(items, key=lambda pair: pair[0])[:8]]

    async def run(self, client):
        while True:
            await self.summary(client)
            await asyncio.sleep(SYNC_INTERVAL)

    async def write(self, client, source_id, payload=None, event_id=None):
        state = self.sources.get(source_id)
        if state is None:
            raise HTTPException(404, "找不到这个日历来源")
        if not state.config.can_write:
            raise HTTPException(403, "这个日历来源只读")
        start, end = self.bounds(datetime.now(timezone.utc))
        async with state.lock:
            if event_id and (state.range_day != start.date() or not state.events):
                await self.refresh_state(client, state, start, end)
            existing = next((item for item in state.events if event_key(item["id"]) == event_id), None) if event_id else None
            if event_id and existing is None:
                raise HTTPException(404, "日程不在当前同步范围内，或已被删除")
            if existing and not existing.get("writable", True):
                raise HTTPException(403, "这条日程只读")
            try:
                if payload is None:
                    await state.adapter.delete(client, existing["id"])
                    state.events = [item for item in state.events if item["id"] != existing["id"]]
                    result = None
                elif existing:
                    changes = payload.model_dump(exclude_none=True)
                    if not changes:
                        return self.public_event(state, existing)
                    latest = await state.adapter.get(client, existing["id"])
                    current = self.public_event(state, latest)
                    if not current["writable"]:
                        raise HTTPException(403, "这条日程只读")
                    valid = {key: current[key] for key in EventInput.model_fields}
                    try:
                        values = event_values(EventInput(**{**valid, **changes}))
                    except ValidationError as error:
                        raise HTTPException(422, "日程字段或时间格式不正确") from error
                    updated = await state.adapter.update(client, latest, values, set(changes))
                    result = self.public_event(state, updated)
                    state.events = [updated if item["id"] == existing["id"] else item for item in state.events]
                else:
                    created = await state.adapter.create(client, event_values(payload))
                    result = self.public_event(state, created)
                    state.events = [*state.events, created]
            except HTTPException:
                raise
            except dav_error.NotFoundError as error:
                raise HTTPException(404, "上游日程已不存在") from error
            except dav_error.AuthorizationError as error:
                raise HTTPException(403, "日历来源没有对应的写入权限") from error
            except (dav_error.ETagMismatchError, dav_error.ScheduleTagMismatchError) as error:
                raise HTTPException(409, "上游日程已变更，请重新同步后再试") from error
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status in {404, 410}:
                    raise HTTPException(404, "上游日程已不存在") from error
                if status in {409, 412}:
                    raise HTTPException(409, "上游日程已变更，请重新同步后再试") from error
                raise HTTPException(502, "日历更新未确认，请检查来源权限或连接") from error
            except Exception as error:
                logger.info("日历更新未确认（%s）", type(error).__name__)
                raise HTTPException(502, "日历更新未确认，请检查来源权限或连接") from error
            await self.refresh_state(client, state, start, end)
            return result


def calendar_router(service):
    router = APIRouter(prefix="/api/calendar/events", tags=["calendar"])

    @router.get("")
    async def list_events(request: Request, response: Response, refresh: bool = False):
        response.headers["Cache-Control"] = "no-store"
        return await service.summary(request.app.state.external_client, refresh)

    @router.post("", status_code=201)
    async def create_event(payload: NewEvent, request: Request):
        return await service.write(request.app.state.external_client, payload.source_id, payload)

    @router.patch("/{source_id}/{event_id}")
    async def edit_event(source_id: str, event_id: str, payload: EventEdit, request: Request):
        return await service.write(request.app.state.external_client, source_id, payload, event_id)

    @router.delete("/{source_id}/{event_id}", status_code=204)
    async def delete_event(source_id: str, event_id: str, request: Request):
        await service.write(request.app.state.external_client, source_id, event_id=event_id)

    return router
