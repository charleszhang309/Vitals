"""Apple 提醒事项同步。中心端保留删除标记与已确认的版本。"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator


APPLE_REMINDERS = "apple_reminders"
MAX_CHANGES = 500
ExternalId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LocalId = Annotated[int, Field(gt=0, strict=True)]


class ReminderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_id: ExternalId
    title: Title | None = None
    done: StrictBool = False
    updated_at: AwareDatetime
    deleted: StrictBool = False

    @model_validator(mode="after")
    def require_title(self):
        if not self.deleted and self.title is None:
            raise ValueError("有效的提醒事项必须提供标题")
        return self


class ReminderAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_id: LocalId
    external_id: ExternalId
    updated_at: AwareDatetime


class RemindersSync(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReminderItem] = Field(default_factory=list, max_length=1000)
    acknowledgements: list[ReminderAck] = Field(default_factory=list, max_length=MAX_CHANGES)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def todo_revision(previous=None):
    current = stamp(datetime.now(timezone.utc))
    if previous and previous >= current:
        return stamp(datetime.fromisoformat(previous) + timedelta(microseconds=1))
    return current


def migrate_reminders(db, today):
    db.execute("BEGIN IMMEDIATE")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(todos)")}
    for name, spec in {
        "external_source": "TEXT",
        "external_id": "TEXT",
        "updated_at": "TEXT",
        "deleted": "INTEGER NOT NULL DEFAULT 0",
        "reminders_synced_at": "TEXT",
    }.items():
        if name not in columns:
            db.execute(f"ALTER TABLE todos ADD COLUMN {name} {spec}")
    # 旧版每日待办只导出当天的记录。升级后新增的待办可跨午夜重试。
    db.execute("""UPDATE todos SET updated_at=day || 'T00:00:00.000000+00:00',
                   reminders_synced_at=CASE WHEN day<? THEN day || 'T00:00:00.000000+00:00' END
                   WHERE updated_at IS NULL""", (today,))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS todo_external_id ON todos(external_source,external_id)")


def acknowledge(db, ack):
    row = db.execute("SELECT * FROM todos WHERE id=?", (ack.local_id,)).fetchone()
    if row is None:
        return
    revision = stamp(ack.updated_at)
    if revision > row["updated_at"]:
        raise HTTPException(422, "确认版本晚于本地待办版本")
    if row["external_id"] and row["external_id"] != ack.external_id:
        raise HTTPException(409, "该待办已经关联另一条提醒事项")
    if db.execute("SELECT 1 FROM todos WHERE external_source=? AND external_id=? AND id<>?",
                  (APPLE_REMINDERS, ack.external_id, ack.local_id)).fetchone():
        raise HTTPException(409, "该提醒事项已关联另一条待办")
    db.execute("UPDATE todos SET external_source=?,external_id=?,reminders_synced_at=? WHERE id=?",
               (APPLE_REMINDERS, ack.external_id, max(revision, row["reminders_synced_at"] or ""), ack.local_id))


def merge_item(db, item, day_for):
    revision = stamp(item.updated_at)
    row = db.execute("SELECT * FROM todos WHERE external_source=? AND external_id=?",
                     (APPLE_REMINDERS, item.external_id)).fetchone()
    day = day_for(item.updated_at)
    if row is None:
        db.execute("""INSERT INTO todos(title,day,done,external_source,external_id,updated_at,deleted,reminders_synced_at)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (item.title or "", day, item.done, APPLE_REMINDERS, item.external_id, revision, item.deleted, revision))
    elif revision > row["updated_at"]:
        db.execute("UPDATE todos SET title=?,day=?,done=?,updated_at=?,deleted=?,reminders_synced_at=? WHERE id=?",
                   (item.title or row["title"], day, item.done, revision, item.deleted, revision, row["id"]))
    elif revision == row["updated_at"] and item.deleted == bool(row["deleted"]) and (
        item.deleted or (item.title == row["title"] and item.done == bool(row["done"]))
    ):
        db.execute("UPDATE todos SET reminders_synced_at=? WHERE id=?", (revision, row["id"]))


def sync_reminders(store, payload):
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    if any(row.updated_at > deadline for row in payload.items):
        raise HTTPException(422, "提醒事项更新时间明显晚于中心端时间")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for ack in payload.acknowledgements:
            acknowledge(db, ack)
        for item in payload.items:
            merge_item(db, item, store.day)
        rows = db.execute("""SELECT id,title,done,external_id,updated_at,deleted FROM todos
                             WHERE ((external_source=? AND (reminders_synced_at IS NULL OR updated_at>reminders_synced_at))
                                 OR (external_source IS NULL AND deleted=0 AND reminders_synced_at IS NULL))
                             ORDER BY id LIMIT ?""", (APPLE_REMINDERS, MAX_CHANGES + 1)).fetchall()
        changes = [
            {"action": "delete" if row["deleted"] else "update" if row["external_id"] else "create",
             "local_id": row["id"], "external_id": row["external_id"], "title": row["title"],
             "done": bool(row["done"]), "updated_at": row["updated_at"], "deleted": bool(row["deleted"])}
            for row in rows[:MAX_CHANGES]
        ]
        return {"changes": changes, "has_more": len(rows) > MAX_CHANGES}
