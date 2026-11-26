import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4
from xml.etree.ElementTree import fromstring
from zoneinfo import ZoneInfo

from icalendar import Calendar
import httpx
import pytest

from backend.calendar_service import CalendarService, EventEdit, NewEvent
from backend.calendar_sources import CalDAVSource, EventCalendar
from backend.config import CalendarConfig, CalendarSource, FinanceConfig, Settings
from backend.life import LifeStore
from backend.main import create_app
from backend.rates import RatePins, Rates
from backend.scratchpad import Scratchpad


def test_life_endpoints_persist_changes_and_reset_daily_state(tmp_path, monkeypatch):
    setting = Settings(health={"timezone": "America/Los_Angeles"})
    app = create_app(setting, tmp_path, static_dir=tmp_path / "no-static")
    scratch = Scratchpad(tmp_path)
    scratch.initialize()
    LifeStore(scratch.connect, ZoneInfo("America/Los_Angeles")).initialize()
    current = datetime(2026, 10, 15, 6, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(LifeStore, "day", lambda self, moment=None: (moment or current).astimezone(self.zone).date().isoformat())

    async def verify():
        nonlocal current
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            for path in ("/api/todos", "/api/habits"):
                item = (await client.post(path, json={"title": "First item"})).json()
                target = f"{path}/{item['id']}"
                assert (await client.patch(target, json={"title": "Renamed"})).status_code == 204
                assert (await client.put(target + "/check", json={"done": True})).status_code == 204
                assert (await client.get(path)).json()["items"] == [{"id": item["id"], "title": "Renamed", "done": True}]
                if path.endswith("habits"):
                    assert (await client.put(target + "/check", json={"done": False})).status_code == 204
                    assert not (await client.get(path)).json()["items"][0]["done"]
                    await client.put(target + "/check", json={"done": True})
            current += timedelta(minutes=2)
            assert not (await client.get("/api/todos")).json()["items"]
            assert (await client.delete("/api/todos/1")).status_code == 404
            assert not (await client.get("/api/habits")).json()["items"][0]["done"]
            assert (await client.delete("/api/habits/1")).status_code == 204
            assert not (await client.get("/api/habits")).json()["items"]
            fields = {"title": "Upcoming", "target_date": "2026-10-20", "note": "Fixture note"}
            saved = await client.post("/api/countdowns", json=fields)
            path = f"/api/countdowns/{saved.json()['id']}"
            assert (await client.get("/api/countdowns")).json()["items"][0]["days"] == 5
            fields["target_date"] = "2026-10-14"
            assert (await client.patch(path, json=fields)).status_code == 204
            assert (await client.get("/api/countdowns")).json()["items"][0]["days"] == -1
            assert (await client.delete(path)).status_code == 204
            sid = str(uuid4())
            payload = {"session_id": sid, "duration_minutes": 25, "completed_at": current.isoformat()}
            assert (await client.post("/api/focus", json=payload)).status_code == 201
            assert (await client.post("/api/focus", json=payload)).json()["total_minutes"] == 25
            assert (await client.patch(f"/api/focus/{sid}", json={"duration_minutes": 15})).status_code == 204
            assert (await client.get("/api/focus")).json()["total_minutes"] == 15
            assert (await client.delete(f"/api/focus/{sid}")).status_code == 204
            assert (await client.get("/api/focus")).json()["items"] == []
            for duration in (0, 181, 2.5, True):
                assert (await client.post("/api/focus", json={**payload, "duration_minutes": duration})).status_code == 422
            assert (await client.post("/api/focus", json={**payload, "duration_minutes": 180})).json()["total_minutes"] == 180
            assert (await client.post("/api/habits", json={"title": "Cross-site"},
                                      headers={"Origin": "https://foreign.test"})).status_code == 403

    asyncio.run(verify())


def test_rates_fetch_once_per_hour_and_serve_stale(monkeypatch):
    moment, calls, offline = 0, [], False
    monkeypatch.setattr("backend.rates.monotonic", lambda: moment)

    def upstream(request):
        assert request.url.params["base"] == "CAD" and request.url.params["quotes"] == "USD,CNY"
        calls.append(request.url.path)
        if offline:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"base": "CAD", "quote": "USD", "date": "2026-01-02", "rate": .7}])

    async def verify():
        nonlocal moment, offline
        rates = Rates(FinanceConfig(base_currency="cad", target_currencies=["usd", "cny", "USD", "CAD"]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            live, again = await asyncio.gather(rates.read(client), rates.read(client))
            assert live == again and len(calls) == 1
            assert live["rates"]["CAD"]["rate"] == 1 and live["rates"]["USD"]["date"] == "2026-01-02"
            moment += 3601
            offline = True
            assert (await rates.read(client))["status"] == "stale"
            assert (await rates.read(client))["rates"] == live["rates"] and len(calls) == 2

    asyncio.run(verify())


@pytest.mark.parametrize("kind", ["google", "outlook"])
def test_cloud_calendar_reads_writes_and_refreshes(kind):
    now = datetime.now(timezone.utc).replace(hour=16, minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=1)
    config = CalendarConfig(sources=[CalendarSource(id="cloud", name="测试日历", type=kind, client_id="fixture-client",
                                                    client_secret="fixture-secret", refresh_token="fixture-refresh")])
    google = kind == "google"
    token_host = "oauth2.googleapis.com" if google else "login.microsoftonline.com"
    fields = ({"summary": "Fixture event", "start": {"dateTime": now.isoformat()}, "end": {"dateTime": end.isoformat()},
               "location": "Fixture room", "description": "Fixture memo"} if google else {
                   "subject": "Fixture event", "start": {"dateTime": now.isoformat(), "timeZone": "UTC"},
                   "end": {"dateTime": end.isoformat(), "timeZone": "UTC"}, "location": {"displayName": "Fixture room"},
                   "body": {"contentType": "text", "content": "Fixture memo"}})
    events = {"one": {"id": "one", **fields}}
    calls = []
    token_calls = 0

    def upstream(request):
        nonlocal token_calls
        calls.append((request.method, request.url.path))
        if request.url.host == token_host:
            token_calls += 1
            assert b"grant_type=refresh_token" in request.content
            assert b"refresh_token=fixture-refresh" in request.content
            return httpx.Response(200, json={"access_token": f"fixture-access-{token_calls}", "expires_in": 3600})
        if request.headers["authorization"] == "Bearer fixture-access-1":
            return httpx.Response(401)
        if request.method == "GET" and request.url.path.endswith(("events", "calendarView")):
            assert ("singleEvents" if google else "startDateTime") in request.url.params
            return httpx.Response(200, json={"items" if google else "value": list(events.values())})
        if request.method == "POST":
            events["two"] = {"id": "two", **json.loads(request.content)}
            return httpx.Response(201, json=events["two"])
        identifier = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            return httpx.Response(200, json=events[identifier])
        if request.method == "PATCH":
            patch_fields = json.loads(request.content)
            assert set(patch_fields) == {"summary" if google else "subject"}
            events[identifier].update(patch_fields)
            return httpx.Response(200, json=events[identifier])
        if request.method == "DELETE":
            del events[identifier]
            return httpx.Response(204)
        raise AssertionError("unexpected provider request")

    async def verify():
        service = CalendarService(config, ZoneInfo("UTC"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await service.summary(client)
            assert token_calls == 2 and result["events"][0]["title"] == "Fixture event"
            assert "fixture-refresh" not in json.dumps(result) and "access_token" not in json.dumps(result)
            assert "notes" not in service.agenda(now)[0] and "location" not in service.agenda(now)[0]
            previous = len(calls)
            assert (await service.summary(client)) == result and len(calls) == previous
            added = await service.write(client, "cloud", NewEvent(source_id="cloud", title="Added", start=now.isoformat(), end=end.isoformat()))
            updated = await service.write(client, "cloud", EventEdit(title="Edited"), added["id"])
            assert updated["title"] == "Edited" and updated["start"] == now.isoformat()
            await service.write(client, "cloud", event_id=added["id"])
            assert len((await service.summary(client))["events"]) == 1
            all_day = await service.write(client, "cloud", NewEvent(source_id="cloud", title="All-day", all_day=True,
                                                                   start=now.date().isoformat(),
                                                                   end=(now.date() + timedelta(days=1)).isoformat()))
            assert all_day["all_day"] and all_day["start"] == now.date().isoformat()

    asyncio.run(verify())


def ics_text(day, recurrence=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Fixture//EN\r\nBEGIN:VEVENT\r\nUID:sample\r\n"
            f"DTSTART:{day}T150000Z\r\nDTEND:{day}T160000Z\r\n{recurrence}"
            "SUMMARY:Fixture event\r\nLOCATION:Fixture place\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def test_ics_subscriptions_expand_and_degrade_without_write(tmp_path):
    now = datetime.now(timezone.utc)
    raw = ics_text(now.strftime("%Y%m%d"), "RRULE:FREQ=DAILY;COUNT=2\r\n")
    sources = CalendarConfig(sources=[CalendarSource(id="feed", name="只读日历", type="ics", url="webcal://calendar.example.invalid/demo.ics")])
    reachable = True

    def upstream(request):
        assert request.url.scheme == "https" and request.method == "GET"
        return httpx.Response(200, text=raw) if reachable else httpx.Response(503)

    async def verify():
        nonlocal reachable
        app = create_app(Settings(calendar=sources), tmp_path, static_dir=tmp_path / "no-static")
        async with (httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client):
            app.state.external_client = upstream_client
            reply = await client.get("/api/calendar/events")
            assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
            result = reply.json()
            assert len(result["events"]) == 2
            assert all(not event["writable"] for event in result["events"])
            assert len({event["id"] for event in result["events"]}) == 2
            assert "calendar.example.invalid" not in json.dumps(result)
            assert (await client.delete(f"/api/calendar/events/feed/{result['events'][0]['id']}")).status_code == 403
            reachable = False
            stale = (await client.get("/api/calendar/events?refresh=1")).json()
            assert stale["events"] == result["events"] and stale["sources"][0]["status"] == "stale"

    asyncio.run(verify())


def test_caldav_edit_delete_and_recurring_occurrence(monkeypatch):
    now = datetime.now(timezone.utc)
    initial_day = now.strftime("%Y%m%d")
    instant = datetime.fromisoformat(f"{now.date().isoformat()}T15:00:00+00:00")
    zone = ZoneInfo("UTC")
    items = {}

    class RemoteEvent:
        def __init__(self, uid, data):
            self.uid, self.data = uid, data

        def save(self, raw_data, no_create):
            assert no_create
            self.data = raw_data

        def delete(self):
            del items[self.uid]

    class RemoteCalendar:
        def save_event(self, data):
            uid = str(Calendar.from_ical(data).get("UID") or Calendar.from_ical(data).walk("VEVENT")[0]["UID"])
            items[uid] = RemoteEvent(uid, f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{data}END:VCALENDAR\r\n")

        def search(self, **params):
            assert params["event"] and not params["expand"]
            return list(items.values())

        def event_by_uid(self, uid):
            return items[uid]

    monkeypatch.setattr(CalDAVSource, "calendars", lambda self: nullcontext([EventCalendar(RemoteCalendar(), "fixture", True, True)]))
    sources = CalendarConfig(sources=[CalendarSource(id="apple", name="测试 iCloud", type="icloud")])
    items["sample"] = RemoteEvent("sample", ics_text(initial_day, "RRULE:FREQ=DAILY;COUNT=2\r\n"))

    async def verify():
        service = CalendarService(sources, zone)
        one = await service.write(None, "apple", NewEvent(source_id="apple", title="Standalone", start=instant.isoformat(),
                                                          end=(instant + timedelta(hours=1)).isoformat()))
        assert any(event["id"] == one["id"] for event in (await service.summary(None))["events"])
        two = await service.write(None, "apple", EventEdit(title="Changed"), one["id"])
        assert two["title"] == "Changed"
        await service.write(None, "apple", event_id=one["id"])
        original, later = (await service.summary(None))["events"]
        updated = await service.write(None, "apple", EventEdit(title="Override"), later["id"])
        assert updated["title"] == "Override"
        assert len(Calendar.from_ical(items["sample"].data).walk("VEVENT")) == 2
        await service.write(None, "apple", event_id=later["id"])
        assert (await service.summary(None))["events"] == [original]

    asyncio.run(verify())


def test_icloud_scans_all_event_calendars_and_writes_to_owner(monkeypatch):
    zone = ZoneInfo("UTC")
    start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    day = start.strftime("%Y%m%d")

    class RemoteCalendar:
        def __init__(self, name, privilege, component="VEVENT"):
            self.id = self.name = name
            self.url = f"https://fixture.invalid/{name}/"
            self.privilege, self.component = privilege, component
            self.calls = []

        def get_supported_components(self):
            return [self.component]

        def get_properties(self, props, *, parse_props):
            assert not parse_props
            return {props[0].tag: fromstring(
                f'<current-user-privilege-set xmlns="DAV:"><privilege><{self.privilege}/></privilege></current-user-privilege-set>'
            )}

        def search(self, **params):
            assert params["event"]
            self.calls.append("search")
            return [self.event_by_uid("sample", reading=True)]

        def event_by_uid(self, uid, *, reading=False):
            assert uid == "sample"
            calendar = self
            if not reading:
                self.calls.append("lookup")

            class RemoteEvent:
                data = ics_text(day).replace("SUMMARY:Fixture event", f"SUMMARY:{calendar.name}")

                def save(self, *, raw_data, no_create):
                    assert no_create
                    assert Calendar.from_ical(raw_data).walk("VEVENT")[0]["SUMMARY"] == "Edited"
                    calendar.calls.append("edit")

                def delete(self):
                    calendar.calls.append("delete")

            return RemoteEvent()

        def save_event(self, data):
            assert Calendar.from_ical(data.decode() if isinstance(data, bytes) else data).get("SUMMARY") == "New"
            self.calls.append("create")

    reminders = RemoteCalendar("Reminders", "write", "VTODO")
    public = RemoteCalendar("Public", "read")
    home = RemoteCalendar("Home", "write")
    work = RemoteCalendar("Work", "write")

    class Principal:
        def calendars(self):
            return [reminders, public, home, work]

    class Connection:
        def principal(self):
            return Principal()

    monkeypatch.setattr("backend.calendar_sources.caldav.DAVClient", lambda **_: nullcontext(Connection()))
    config = CalendarSource(id="apple", name="iCloud", type="icloud", apple_id="fixture", app_password="fixture")
    provider = CalDAVSource(config, zone)
    result = provider.scan(start, end)
    assert len({event["id"] for event in result}) == 3
    assert [event["title"] for event in result] == ["Public", "Home", "Work"]
    assert [event["writable"] for event in result] == [False, True, True]
    fields = {"title": "New", "location": "", "notes": "", "start": start, "end": end, "all_day": False}
    created = provider.create_sync(fields)
    assert created["id"].partition("\x1f")[0] == result[1]["id"].partition("\x1f")[0]
    identifier = result[2]["id"]
    assert provider.update_sync(identifier, {"title": "Edited"}, {"title"})["title"] == "Edited"
    provider.delete_sync(identifier)
    assert reminders.calls == []
    assert public.calls == ["search"]
    assert home.calls == ["search", "create"]
    assert work.calls == ["search", "lookup", "edit", "lookup", "delete"]
    single = CalDAVSource(config.model_copy(update={"calendar_id": "Work"}), zone)
    assert [event["id"] for event in single.scan(start, end)] == [identifier]


def test_pinned_rates_order_limit_and_persistence(tmp_path):
    settings = Settings()
    app = create_app(settings, tmp_path)
    store = Scratchpad(tmp_path)
    store.initialize()
    pins = RatePins(store.connect, settings.finance)
    pins.initialize()

    def upstream(request):
        base = request.url.params["base"]
        return httpx.Response(200, json=[{"base": base, "quote": target, "rate": 2.5, "date": "2026-01-02"}
                                        for target in request.url.params["quotes"].split(",")])

    async def verify():
        async with (httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client):
            app.state.external_client = external
            snapshot = (await client.get("/api/rates")).json()
            assert [(item["base"], item["target"]) for item in snapshot["pins"]] == [("CAD", "CNY"), ("CAD", "USD")]
            assert snapshot["pins"][0]["rate"] == 2.5
            assert {"JPY", "HKD", "CAD"} <= set(snapshot["currencies"])
            assert (await client.get("/api/rates/quote?base=JPY&target=CAD")).json()["rate"] == 2.5
            assert (await client.get("/api/rates/quote?base=USD&target=USD")).json()["rate"] == 1
            for target in ("JPY", "GBP", "EUR"):
                assert (await client.post("/api/rates/pins", json={"base": "USD", "target": target})).status_code == 200
            full = (await client.post("/api/rates/pins", json={"base": "CAD", "target": "EUR"}))
            assert full.status_code == 409
            assert (await client.post("/api/rates/pins", json={"base": "CAD", "target": "USD"})).status_code == 200
            moved = (await client.patch("/api/rates/pins/CAD/USD", json={"direction": "up"})).json()
            assert moved["pins"][0] == {"base": "CAD", "target": "USD"}
            assert (await client.patch("/api/rates/pins/CAD/USD", json={"direction": "down"})).json()["pins"][0]["target"] == "CNY"
            assert (await client.delete("/api/rates/pins/CAD/CNY")).status_code == 200
            assert (await client.get("/api/rates")).json()["pins"][0]["target"] == "USD"
            reloaded = RatePins(Scratchpad(tmp_path).connect, settings.finance)
            reloaded.initialize()
            assert len(reloaded.list()) == 4 and reloaded.list()[0]["target"] == "USD"
            for pair in reloaded.list():
                assert (await client.delete(f"/api/rates/pins/{pair['base']}/{pair['target']}")).status_code == 200
            reloaded.initialize()
            assert not reloaded.list()

    asyncio.run(verify())
