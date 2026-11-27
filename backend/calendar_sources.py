"""日历提供方适配。OAuth 端点固定，订阅地址只能由服务器配置指定。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import logging
import re
from time import monotonic
from urllib.parse import quote, urlsplit
from uuid import uuid4

import caldav
from caldav.elements import dav
from caldav.lib import error as dav_error
import httpx
from icalendar import Calendar, Event
import recurring_ical_events


GOOGLE_ORIGIN = "https://www.googleapis.com"
GRAPH_ORIGIN = "https://graph.microsoft.com"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
OUTLOOK_SCOPE = "https://graph.microsoft.com/Calendars.ReadWrite offline_access"
RECURRENCE_SEPARATOR = "\x1e"
CALENDAR_SEPARATOR = "\x1f"
CALDAV_REQUEST_TIMEOUT = 8
CALDAV_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="caldav")
logger = logging.getLogger(__name__)

# CalDAV can include response bodies and account URLs in its own diagnostic logs.
logging.getLogger("caldav").setLevel(logging.CRITICAL + 1)
dav_error.debug_dump_communication = False


def log_calendar_failure(log, config, stage, error):
    status = None
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    elif isinstance(error, dav_error.DAVError):
        status = getattr(error, "status", None)
    log.info("日历来源 %s (%s)：%s 失败（%s，HTTP %s）", config.id, config.type, stage, type(error).__name__,
             status if type(status) is int and 100 <= status <= 599 else "—")


def secret(value):
    return value.get_secret_value().strip() if value else ""


def aware(value, zone):
    raw = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return raw if raw.utcoffset() is not None else raw.replace(tzinfo=zone)


def text(value, size):
    return str(value or "")[:size]


def event_record(identifier, title, start, end, all_day, location, notes, **details):
    if not identifier or len(identifier) > 1200:
        raise ValueError("invalid event id")
    if all_day != (not isinstance(start, datetime) and not isinstance(end, datetime)) or end < start:
        raise ValueError("invalid calendar interval")
    return {"id": identifier, "title": text(title, 300).strip() or "未命名日程", "start": start, "end": end,
            "all_day": all_day, "location": text(location, 500), "notes": text(notes, 8000), **details}


class OAuthSource:
    def __init__(self, config, zone):
        self.config = config
        self.zone = zone
        self.google = config.type == "google"
        self.origin = GOOGLE_ORIGIN if self.google else GRAPH_ORIGIN
        if self.google:
            self.root = f"/calendar/v3/calendars/{quote(config.calendar_id or 'primary', safe='')}/events"
        else:
            self.calendar = f"/v1.0/me/calendars/{quote(config.calendar_id, safe='')}" if config.calendar_id else "/v1.0/me"
            self.root = self.calendar + "/events"
        self.token = ""
        self.refresh = secret(config.refresh_token)
        self.expires = 0.0

    async def access(self, client):
        if self.token and monotonic() < self.expires:
            return self.token
        client_id = secret(self.config.client_id)
        client_secret = secret(self.config.client_secret)
        if not client_id or not self.refresh or (self.google and not client_secret):
            raise ValueError("calendar OAuth credentials incomplete")
        form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": self.refresh}
        if client_secret:
            form["client_secret"] = client_secret
        endpoint = GOOGLE_TOKEN
        if not self.google:
            endpoint = f"https://login.microsoftonline.com/{self.config.tenant}/oauth2/v2.0/token"
            form["scope"] = OUTLOOK_SCOPE
        reply = await client.post(endpoint, data=form, timeout=8, follow_redirects=False)
        reply.raise_for_status()
        token = reply.json()
        self.expires = monotonic() + max(0, int(token.get("expires_in", 3600)) - 60)
        self.refresh = token.get("refresh_token") or self.refresh
        self.token = token["access_token"]
        return self.token

    async def request(self, client, method, url, **options):
        if url.startswith("/"):
            url = self.origin + url
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            raise ValueError("unexpected calendar pagination origin")
        headers = options.pop("headers", {})
        if not self.google:
            headers["Prefer"] = f'outlook.timezone="{self.zone.key}"'
        for attempt in range(2):
            headers["Authorization"] = f"Bearer {await self.access(client)}"
            reply = await client.request(method, url, headers=headers, timeout=8, follow_redirects=False, **options)
            if reply.status_code != 401 or attempt:
                break
            self.token = ""
        reply.raise_for_status()
        return None if reply.status_code == 204 else reply.json()

    def normalize(self, event):
        start, end = event["start"], event["end"]
        if self.google:
            all_day = "date" in start
            begin, finish = (date.fromisoformat(mark["date"]) if all_day else aware(mark["dateTime"], self.zone) for mark in (start, end))
            location, notes = event.get("location"), event.get("description")
            writable = event.get("eventType", "default") == "default" and not event.get("locked")
        else:
            all_day = bool(event.get("isAllDay"))
            begin, finish = (self.graph_time(mark, all_day) for mark in (start, end))
            location = (event.get("location") or {}).get("displayName")
            body = event.get("body") or {}
            notes = body.get("content") if body.get("contentType", "").lower() == "text" else event.get("bodyPreview")
            writable = True
        return event_record(event["id"], event.get("summary" if self.google else "subject"), begin, finish, all_day,
                            location, notes, writable=writable, etag=event.get("etag" if self.google else "@odata.etag"))

    def graph_time(self, mark, all_day):
        # Graph 的 dateTime 通常无偏移，timeZone 或 Prefer 头决定其时区。
        zone = timezone.utc if mark.get("timeZone", "").upper() == "UTC" else self.zone
        return date.fromisoformat(mark["dateTime"][:10]) if all_day else aware(mark["dateTime"], zone)

    async def list(self, client, start, end):
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "maxResults": 250,
                  "orderBy": "startTime"} if self.google else {
            "startDateTime": start.isoformat(), "endDateTime": end.isoformat(), "$top": 100,
        }
        url = self.root if self.google else self.calendar + "/calendarView"
        output = []
        for _ in range(12):
            page = await self.request(client, "GET", url, params=params)
            for row in page.get("items" if self.google else "value", []):
                if row.get("status") == "cancelled" or row.get("isCancelled"):
                    continue
                try:
                    output.append(self.normalize(row))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if self.google and page.get("nextPageToken"):
                params["pageToken"] = page["nextPageToken"]
            elif not self.google and page.get("@odata.nextLink"):
                url, params = page["@odata.nextLink"], None
            else:
                return output
        raise ValueError("calendar event result exceeds paging limit")

    def payload(self, fields, changes):
        keys = ({"title": "summary", "location": "location", "notes": "description"} if self.google else
                {"title": "subject", "location": "location", "notes": "body"})
        result = {remote: fields[key] for key, remote in keys.items() if key in changes}
        if not self.google:
            if "location" in result:
                result["location"] = {"displayName": result["location"]}
            if "body" in result:
                result["body"] = {"contentType": "text", "content": result["body"]}
        if changes & {"start", "end", "all_day"}:
            all_day = fields["all_day"]
            for key in ("start", "end"):
                value = fields[key]
                if self.google:
                    result[key] = {"date" if all_day else "dateTime": value.isoformat()}
                elif all_day:
                    result[key] = {"dateTime": f"{value.isoformat()}T00:00:00", "timeZone": self.zone.key}
                else:
                    result[key] = {"dateTime": value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), "timeZone": "UTC"}
            if not self.google:
                result["isAllDay"] = all_day
        return result

    def event_url(self, identifier):
        # Graph 的个人日程 ID 在 /me/events 中通用；列表和新建仍限定目标日历。
        return (self.root if self.google else "/v1.0/me/events") + "/" + quote(identifier, safe="")

    async def get(self, client, identifier):
        return self.normalize(await self.request(client, "GET", self.event_url(identifier)))

    async def create(self, client, fields):
        return self.normalize(await self.request(client, "POST", self.root, json=self.payload(fields, fields.keys())))

    async def update(self, client, event, fields, changes):
        headers = {"If-Match": event["etag"]} if event.get("etag") else {}
        reply = await self.request(client, "PATCH", self.event_url(event["id"]), headers=headers, json=self.payload(fields, changes))
        return self.normalize(reply) if reply else await self.get(client, event["id"])

    async def delete(self, client, identifier):
        await self.request(client, "DELETE", self.event_url(identifier))


def recurrence_key(value):
    if isinstance(value, datetime) and value.utcoffset() is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def ical_event(component, zone, recurring=True):
    start = component.decoded("DTSTART")
    end = component.decoded("DTEND", None)
    all_day = not isinstance(start, datetime)
    if end is None:
        end = start + component.decoded("DURATION", timedelta(days=1) if all_day else timedelta())
    if not all_day:
        start, end = aware(start, zone), aware(end, zone)
    identifier = str(component["UID"])
    if recurring and (instance := component.decoded("RECURRENCE-ID", None)):
        identifier += RECURRENCE_SEPARATOR + recurrence_key(instance)
    return event_record(identifier, component.get("SUMMARY"), start, end, all_day,
                        component.get("LOCATION"), component.get("DESCRIPTION"))


def occurrences(document, start, end, zone):
    events = []
    recurring = {str(item.get("UID")) for item in document.walk("VEVENT")
                 if any(prop in item for prop in ("RRULE", "RDATE", "RECURRENCE-ID"))}
    for component in recurring_ical_events.of(document, skip_bad_series=True).between(start, end):
        if component.get("STATUS") == "CANCELLED":
            continue
        try:
            events.append(ical_event(component, zone, str(component.get("UID")) in recurring))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return events


class ICSFeed:
    def __init__(self, config, zone):
        self.config = config
        self.zone = zone

    async def list(self, client, start, end):
        url = re.sub(r"^webcal://", "https://", secret(self.config.url), flags=re.IGNORECASE)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("calendar subscription requires an HTTPS URL")
        async with client.stream("GET", url, timeout=8, follow_redirects=False) as reply:
            reply.raise_for_status()
            raw = bytearray()
            async for part in reply.aiter_bytes():
                raw.extend(part)
                if len(raw) > 2 * 1024**2:
                    raise ValueError("calendar feed too large")
        return await asyncio.to_thread(lambda: occurrences(Calendar.from_ical(bytes(raw)), start, end, self.zone))


@dataclass(frozen=True)
class EventCalendar:
    calendar: object
    identifier: str
    can_create: bool
    can_edit: bool


class CalDAVSource:
    def __init__(self, config, zone):
        self.config = config
        self.zone = zone

    @contextmanager
    def diagnose(self, stage):
        try:
            yield
        except Exception as error:
            log_calendar_failure(logger, self.config, stage, error)
            raise

    @contextmanager
    def calendars(self):
        apple_id, password = secret(self.config.apple_id), secret(self.config.app_password)
        if not apple_id or not password:
            with self.diagnose("检查凭据"):
                raise ValueError("missing iCloud credentials")
        with self.diagnose("初始化 CalDAV 客户端"):
            client = caldav.DAVClient(
                url="https://caldav.icloud.com", username=apple_id, password=password,
                timeout=CALDAV_REQUEST_TIMEOUT, rate_limit_handle=False, enable_rfc6764=False,
            )
        with client as connection:
            with self.diagnose("查找 iCloud 日历"):
                remote_calendars = connection.principal().calendars()
            target = self.config.calendar_id
            calendars = []
            failed = None
            for item in remote_calendars:
                if target and target not in {item.name, str(item.id), str(item.url)}:
                    continue
                try:
                    if "VEVENT" not in item.get_supported_components():
                        continue
                    create, edit = self.permissions(item)
                except (dav_error.DAVError, OSError) as error:
                    failed = error
                    log_calendar_failure(logger, self.config, "读取子日历类型与权限", error)
                    continue
                identifier = sha256(str(item.url).rstrip("/").encode()).hexdigest()[:32]
                calendars.append(EventCalendar(item, identifier, create, edit))
            if failed and not calendars:
                raise failed
            if target and not calendars:
                with self.diagnose("匹配配置的日历"):
                    raise ValueError("iCloud calendar not found")
            yield calendars

    @staticmethod
    def permissions(calendar):
        prop = dav.CurrentUserPrivilegeSet()
        try:
            node = calendar.get_properties([prop], parse_props=False).get(prop.tag)
        except dav_error.DAVError:
            node = None
        if node is None:
            # 老服务端可以省略权限属性，写入时由 CalDAV 服务端最终判断。
            return True, True
        privileges = {child.tag.rsplit("}", 1)[-1] for privilege in node for child in privilege if isinstance(child.tag, str)}
        write = bool(privileges & {"all", "write"})
        return write or "bind" in privileges, write or {"write-content", "unbind"}.issubset(privileges)

    @staticmethod
    def scoped(event, source):
        return {**event, "id": source.identifier + CALENDAR_SEPARATOR + event["id"], "writable": source.can_edit}

    @staticmethod
    def owner(calendars, identifier):
        location, _, event_id = identifier.partition(CALENDAR_SEPARATOR)
        source = next((item for item in calendars if item.identifier == location), None)
        if source is None or not event_id:
            raise dav_error.NotFoundError("iCloud event calendar not found")
        return source, event_id

    def scan(self, start, end):
        result = []
        failed = None
        succeeded = False
        with self.calendars() as calendars:
            for source in calendars:
                try:
                    events = source.calendar.search(start=start, end=end, expand=False, event=True)
                    for event in events:
                        try:
                            found = occurrences(Calendar.from_ical(event.data), start, end, self.zone)
                            result.extend(self.scoped(entry, source) for entry in found)
                        except (ValueError, TypeError):
                            continue
                    succeeded = True
                except (dav_error.DAVError, OSError) as error:
                    failed = error
                    log_calendar_failure(logger, self.config, "读取子日历日程", error)
        if failed and not succeeded:
            raise failed
        return result

    @staticmethod
    def fill(component, fields, changes):
        mapping = {"title": "SUMMARY", "location": "LOCATION", "notes": "DESCRIPTION"}
        for key, prop in mapping.items():
            if key in changes:
                component.pop(prop, None)
                component.add(prop, fields[key])
        if changes & {"start", "end", "all_day"}:
            for prop in ("DTSTART", "DTEND", "DURATION"):
                component.pop(prop, None)
            component.add("DTSTART", fields["start"])
            component.add("DTEND", fields["end"])
        component.pop("DTSTAMP", None)
        component.add("DTSTAMP", datetime.now(timezone.utc))

    def create_sync(self, fields):
        with self.calendars() as calendars:
            component = Event()
            component.add("UID", str(uuid4()))
            self.fill(component, fields, fields.keys())
            for source in calendars:
                if source.can_create:
                    try:
                        source.calendar.save_event(component.to_ical().decode())
                        return self.scoped(ical_event(component, self.zone), source)
                    except dav_error.AuthorizationError:
                        continue
            raise dav_error.AuthorizationError("no writable iCloud event calendar")

    def locate(self, calendar, identifier):
        uid, _, recurrence = identifier.partition(RECURRENCE_SEPARATOR)
        resource = calendar.event_by_uid(uid)
        document = Calendar.from_ical(resource.data)
        originals = document.walk("VEVENT")
        master = next(item for item in originals if "RECURRENCE-ID" not in item)
        if not recurrence:
            if any(prop in master for prop in ("RRULE", "RDATE")):
                raise ValueError("calendar event changed into a recurrence")
            return resource, document, master, master
        old = next((item for item in originals if "RECURRENCE-ID" in item and recurrence_key(item.decoded("RECURRENCE-ID")) == recurrence), None)
        if old is not None:
            return resource, document, master, old
        point = datetime.fromisoformat(recurrence) if "T" in recurrence else date.fromisoformat(recurrence)
        instance = next(item for item in recurring_ical_events.of(document).at(point)
                        if item.decoded("RECURRENCE-ID", None) and recurrence_key(item.decoded("RECURRENCE-ID")) == recurrence)
        return resource, document, master, deepcopy(instance)

    def read_sync(self, identifier):
        with self.calendars() as calendars:
            source, event_id = self.owner(calendars, identifier)
            return self.scoped(ical_event(self.locate(source.calendar, event_id)[-1], self.zone), source)

    def update_sync(self, identifier, fields, changes):
        with self.calendars() as calendars:
            source, event_id = self.owner(calendars, identifier)
            if not source.can_edit:
                raise dav_error.AuthorizationError("iCloud event calendar is read-only")
            resource, document, master, component = self.locate(source.calendar, event_id)
            self.fill(component, fields, changes)
            component["SEQUENCE"] = 1 + int(component.get("SEQUENCE", master.get("SEQUENCE", 0)))
            if not any(item is component for item in document.subcomponents):
                document.add_component(component)
            resource.save(raw_data=document.to_ical(), no_create=True)
            return self.scoped(ical_event(component, self.zone), source)

    def delete_sync(self, identifier):
        with self.calendars() as calendars:
            source, event_id = self.owner(calendars, identifier)
            if not source.can_edit:
                raise dav_error.AuthorizationError("iCloud event calendar is read-only")
            resource, document, master, component = self.locate(source.calendar, event_id)
            if "RECURRENCE-ID" not in component:
                resource.delete()
                return
            master.add("EXDATE", component.decoded("RECURRENCE-ID"))
            document.subcomponents = [item for item in document.subcomponents if item is not component]
            resource.save(raw_data=document.to_ical(), no_create=True)

    @staticmethod
    async def run_in_worker(action, *args):
        return await asyncio.get_running_loop().run_in_executor(CALDAV_WORKERS, action, *args)

    async def list(self, _client, start, end):
        return await self.run_in_worker(self.scan, start, end)

    async def get(self, _client, identifier):
        return await self.run_in_worker(self.read_sync, identifier)

    async def create(self, _client, fields):
        return await self.run_in_worker(self.create_sync, fields)

    async def update(self, _client, event, fields, changes):
        return await self.run_in_worker(self.update_sync, event["id"], fields, changes)

    async def delete(self, _client, identifier):
        await self.run_in_worker(self.delete_sync, identifier)
