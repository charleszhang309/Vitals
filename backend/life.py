"""今日待办、微习惯、倒数日和专注记录。"""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from .reminders import APPLE_REMINDERS, RemindersSync, migrate_reminders, sync_reminders, todo_revision

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title


class CheckInput(BaseModel):
    done: bool


class CountdownInput(TaskInput):
    target_date: date
    note: str = Field(default="", max_length=2000)


class FocusEdit(BaseModel):
    duration_minutes: int = Field(ge=1, le=180, strict=True)


class FocusInput(FocusEdit):
    session_id: UUID
    completed_at: datetime | None = None


class LifeStore:
    def __init__(self, connect, zone: ZoneInfo):
        self.connect = connect
        self.zone = zone

    def day(self, moment=None):
        return (moment or datetime.now(timezone.utc)).astimezone(self.zone).date().isoformat()

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, day TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS todos_day ON todos(day);
                CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS habit_logs (
                    habit_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY (habit_id, day)
                );
                CREATE TABLE IF NOT EXISTS countdowns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, target_date TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS focus_sessions (
                    id TEXT PRIMARY KEY, duration_minutes INTEGER NOT NULL, completed_at TEXT NOT NULL, day TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS focus_day ON focus_sessions(day);
            """)
            migrate_reminders(db, self.day())

    def tasks(self, kind):
        today = self.day()
        with self.connect() as db:
            if kind == "todos":
                rows = db.execute("""SELECT id,title,done,external_source FROM todos
                                     WHERE deleted=0 AND (day=? OR (external_source=? AND done=0))
                                     ORDER BY id""", (today, APPLE_REMINDERS))
            else:
                rows = db.execute("""SELECT h.id,h.title,l.habit_id IS NOT NULL AS done FROM habits h
                                     LEFT JOIN habit_logs l ON l.habit_id=h.id AND l.day=? ORDER BY h.id""", (today,))
            items = [dict(row) for row in rows]
            for item in items:
                item["done"] = bool(item["done"])
                if item.get("external_source") is None:
                    item.pop("external_source", None)
            return {"date": today, "items": items}

    def add_task(self, kind, title):
        with self.connect() as db:
            if kind == "todos":
                item = db.execute("INSERT INTO todos(title,day,updated_at) VALUES (?,?,?)", (title, self.day(), todo_revision()))
            else:
                item = db.execute("INSERT INTO habits(title) VALUES (?)", (title,))
            return {"id": item.lastrowid, "title": title, "done": False}

    def change_task(self, kind, item_id, *, title=None, done=None, delete=False):
        day = self.day()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "todos":
                row = db.execute("""SELECT updated_at,title,done FROM todos
                                    WHERE id=? AND deleted=0 AND (day=? OR (external_source=? AND done=0))""",
                                 (item_id, day, APPLE_REMINDERS)).fetchone()
                if row is None:
                    raise HTTPException(404, "找不到这条事项")
                next_title = row["title"] if title is None else title
                next_done = row["done"] if done is None else done
                if delete or (next_title, next_done) != (row["title"], row["done"]):
                    db.execute("UPDATE todos SET title=?,done=?,day=?,updated_at=?,deleted=? WHERE id=?",
                               (next_title, next_done, day, todo_revision(row["updated_at"]), delete, item_id))
                return
            if not db.execute("SELECT 1 FROM habits WHERE id=?", (item_id,)).fetchone():
                raise HTTPException(404, "找不到这条事项")
            if delete:
                db.execute("DELETE FROM habits WHERE id=?", (item_id,))
                db.execute("DELETE FROM habit_logs WHERE habit_id=?", (item_id,))
            elif title is not None:
                db.execute("UPDATE habits SET title=? WHERE id=?", (title, item_id))
            elif done:
                db.execute("INSERT OR IGNORE INTO habit_logs(habit_id,day) VALUES (?,?)", (item_id, day))
            else:
                db.execute("DELETE FROM habit_logs WHERE habit_id=? AND day=?", (item_id, day))

    def countdowns(self):
        today = date.fromisoformat(self.day())
        with self.connect() as db:
            rows = db.execute("SELECT * FROM countdowns ORDER BY target_date,id")
            return {"date": today, "items": [{**dict(row), "days": (date.fromisoformat(row["target_date"]) - today).days} for row in rows]}

    def change_countdown(self, item_id=None, payload=None):
        with self.connect() as db:
            if payload is None:
                cursor = db.execute("DELETE FROM countdowns WHERE id=?", (item_id,))
            else:
                fields = (payload.title, payload.target_date.isoformat(), payload.note.strip())
                if item_id is None:
                    cursor = db.execute("INSERT INTO countdowns(title,target_date,note,created_at) VALUES (?,?,?,?)",
                                        (*fields, datetime.now(timezone.utc).isoformat()))
                    return {"id": cursor.lastrowid}
                cursor = db.execute("UPDATE countdowns SET title=?,target_date=?,note=? WHERE id=?", (*fields, item_id))
            if cursor.rowcount == 0:
                raise HTTPException(404, "找不到这个倒数日")

    def focus(self):
        today = self.day()
        with self.connect() as db:
            rows = db.execute("SELECT id,duration_minutes,completed_at FROM focus_sessions WHERE day=? ORDER BY completed_at DESC", (today,))
            items = [dict(row) for row in rows]
        return {"date": today, "total_minutes": sum(item["duration_minutes"] for item in items), "items": items}

    def record_focus(self, payload):
        now = datetime.now(timezone.utc)
        completed = payload.completed_at or now
        if completed.utcoffset() is None or completed > now + timedelta(minutes=2):
            raise HTTPException(422, "专注完成时间必须包含时区，且不能明显晚于当前时间")
        completed = min(completed.astimezone(timezone.utc), now)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO focus_sessions(id,duration_minutes,completed_at,day) VALUES (?,?,?,?)",
                       (str(payload.session_id), payload.duration_minutes, completed.isoformat(), self.day(completed)))

    def change_focus(self, session_id, minutes=None):
        with self.connect() as db:
            if minutes is None:
                cursor = db.execute("DELETE FROM focus_sessions WHERE id=?", (str(session_id),))
            else:
                cursor = db.execute("UPDATE focus_sessions SET duration_minutes=? WHERE id=?", (minutes, str(session_id)))
            if cursor.rowcount == 0:
                raise HTTPException(404, "找不到这条专注记录")


def life_router(store, authenticate):
    router = APIRouter(prefix="/api", tags=["life"])

    def mount_tasks(kind):
        @router.get(f"/{kind}", name=f"list_{kind}")
        def list_tasks(response: Response):
            response.headers["Cache-Control"] = "no-store"
            return store.tasks(kind)

        @router.post(f"/{kind}", status_code=201, name=f"create_{kind}")
        def create_task(payload: TaskInput):
            return store.add_task(kind, payload.title)

        @router.patch(f"/{kind}/{{item_id}}", status_code=204, name=f"rename_{kind}")
        def rename_task(item_id: int, payload: TaskInput):
            store.change_task(kind, item_id, title=payload.title)

        @router.put(f"/{kind}/{{item_id}}/check", status_code=204, name=f"check_{kind}")
        def check_task(item_id: int, payload: CheckInput):
            store.change_task(kind, item_id, done=payload.done)

        @router.delete(f"/{kind}/{{item_id}}", status_code=204, name=f"delete_{kind}")
        def delete_task(item_id: int):
            store.change_task(kind, item_id, delete=True)

    mount_tasks("todos")
    mount_tasks("habits")

    @router.post("/todos/sync/reminders", dependencies=[Depends(authenticate)])
    def reminders(payload: RemindersSync):
        return sync_reminders(store, payload)

    @router.get("/countdowns")
    def countdowns(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.countdowns()

    @router.post("/countdowns", status_code=201)
    def add_countdown(payload: CountdownInput):
        return store.change_countdown(payload=payload)

    @router.patch("/countdowns/{item_id}", status_code=204)
    def edit_countdown(item_id: int, payload: CountdownInput):
        store.change_countdown(item_id, payload)

    @router.delete("/countdowns/{item_id}", status_code=204)
    def remove_countdown(item_id: int):
        store.change_countdown(item_id)

    @router.get("/focus")
    def focus(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.focus()

    @router.post("/focus", status_code=201)
    async def record_focus(payload: FocusInput):
        await run_in_threadpool(store.record_focus, payload)
        return await run_in_threadpool(store.focus)

    @router.patch("/focus/{session_id}", status_code=204)
    def edit_focus(session_id: UUID, payload: FocusEdit):
        store.change_focus(session_id, payload.duration_minutes)

    @router.delete("/focus/{session_id}", status_code=204)
    def delete_focus(session_id: UUID):
        store.change_focus(session_id)

    return router
