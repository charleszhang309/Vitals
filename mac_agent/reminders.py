"""Exchange reminder deltas with Vitals and apply them to one Apple list."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import re

from .macos import javascript
from .state import load_state, save_state

SYNC_PATH = "/api/todos/sync/reminders"
logger = logging.getLogger(__name__)
MARKER = re.compile(r"^\[Vitals sync ID: ([A-Za-z0-9_:-]{1,128})\]")
REMINDERS_SCRIPT = """
function run(args) {
    const input = JSON.parse(args[0]);
    const app = Application("com.apple.reminders");
    let list;
    if (input.name === null) {
        list = app.defaultList;
    } else {
        const matches = app.lists.whose({name: input.name})();
        if (matches.length !== 1) throw new Error("Expected exactly one Reminders list with that name");
        list = matches[0];
    }
    const listId = list.id();
    if (typeof listId !== "string" || !listId) throw new Error("No default Reminders list");
    if (input.list_id && listId !== input.list_id) throw new Error("Selected Reminders list changed");
    if (input.action === "snapshot") {
        const ids = list.reminders.id();
        const titles = list.reminders.name();
        const done = list.reminders.completed();
        const bodies = list.reminders.body();
        const revisions = list.reminders.modificationDate();
        if (![ids, titles, done, bodies, revisions].every(v => Array.isArray(v) && v.length === ids.length)) {
            throw new Error("Incomplete Reminders snapshot");
        }
        return JSON.stringify({list_id: listId, items: ids.map((id, i) => ({
            id, title: titles[i], done: done[i], body: bodies[i], updated_at: revisions[i]
        }))});
    }
    const expected = input.expected;
    if (expected !== null) {
        const matches = list.reminders.whose({id: expected.native_id})();
        if (matches.length !== 1) return "false";
        const item = matches[0];
        if (item.name() !== expected.title || item.completed() !== expected.done) return "false";
        if (input.action === "delete") {
            app.delete(item);
        } else {
            if (expected.title !== input.title) item.name = input.title;
            if (expected.done !== input.done) item.completed = input.done;
        }
    } else {
        if (input.action !== "upsert") throw new Error("Unexpected reminder operation");
        const item = app.Reminder({name: input.title, body: input.marker, completed: input.done});
        list.reminders.push(item);
    }
    return "true";
}
"""


def fields(item):
    return {"title": item["title"].strip()[:200].rstrip(), "done": item["done"]}


def revision(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("提醒事项时间缺少时区")
    return timestamp.astimezone(timezone.utc)


def snapshot_changes(seen, items):
    changes = {}
    for key, item in items.items():
        current = fields(item)
        old = seen.get(key)
        if not current["title"] or (old and fields(old) == current):
            continue
        updated = revision(item.get("updated_at"))
        if old:
            updated = max(updated, revision(old["updated_at"]) + timedelta(microseconds=1))
        changes[key] = {"external_id": key, **current, "updated_at": updated.isoformat()}
    for key in seen.keys() - items.keys():
        updated = max(revision(), revision(seen[key]["updated_at"]) + timedelta(microseconds=1))
        changes[key] = {"external_id": key, "updated_at": updated.isoformat(), "deleted": True}
    return changes


def validate_response(response):
    if not isinstance(response, dict) or type(response.get("has_more")) is not bool:
        raise ValueError("同步响应缺少 has_more")
    changes = response.get("changes")
    if not isinstance(changes, list) or len(changes) > 500:
        raise ValueError("同步响应中 changes 无效")
    ids = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("同步响应含无效事项")
        action = change.get("action")
        local_id = change.get("local_id")
        key = change.get("external_id")
        if (action not in ("create", "update", "delete") or type(local_id) is not int
                or local_id <= 0 or local_id in ids
                or (key is not None if action == "create" else not isinstance(key, str) or not key)
                or type(change.get("done")) is not bool or not isinstance(change.get("title"), str)
                or change.get("deleted") is not (action == "delete") or not isinstance(change.get("updated_at"), str)):
            raise ValueError("同步响应含无效事项")
        if action != "delete" and not fields(change)["title"]:
            raise ValueError("同步响应中的有效事项缺少标题")
        revision(change["updated_at"])
        ids.add(local_id)
    return changes


class AppleReminders:
    def __init__(self, name):
        self.name = name

    def call(self, **payload):
        return json.loads(javascript(REMINDERS_SCRIPT, json.dumps({"name": self.name, **payload}), timeout=20))

    def snapshot(self):
        data = self.call(action="snapshot")
        list_id = data["list_id"]
        if not isinstance(list_id, str) or not list_id:
            raise ValueError("macOS 返回了无效的提醒事项列表 ID")
        items = {}
        for item in data["items"]:
            native_id = item["id"]
            if (not isinstance(native_id, str) or not native_id
                    or not isinstance(item.get("title"), str) or type(item.get("done")) is not bool):
                raise ValueError("macOS 返回了无效的提醒事项")
            marker = MARKER.match(item.get("body") or "")
            key = marker[1] if marker else "mac-" + sha256(native_id.encode()).hexdigest()
            if key in items:
                raise ValueError("列表中有重复的 Vitals 同步 ID")
            items[key] = {"native_id": native_id, "title": item["title"],
                          "done": item["done"], "updated_at": item.get("updated_at")}
        return list_id, items

    def apply(self, list_id, key, desired, expected):
        args = {"list_id": list_id, "expected": expected}
        if desired is None:
            return self.call(action="delete", **args)
        return self.call(action="upsert", marker=f"[Vitals sync ID: {key}]", **desired, **args)


def finish_pending(path, state, reminders, list_id, scope):
    actual_list, current = reminders.snapshot()
    if actual_list != list_id:
        raise RuntimeError("已选的提醒事项列表发生变化，暂停应用待处理的同步")
    for change in state["pending"]:
        action = change["action"]
        local_id = change["local_id"]
        external_id = change["external_id"] or f"vitals-{scope}-{local_id}"
        item = current.get(external_id)
        old = state["seen"].get(external_id)
        desired = None if action == "delete" else fields(change)
        observed = fields(item) if item else None
        if observed != desired and not (action == "create" and item):
            # Leave concurrent local edits available for the next upload.
            if observed != (fields(old) if old else None):
                continue
            if item is None and action == "update":
                logger.warning("找不到关联的 Apple 提醒事项，暂缓更新 Vitals 待办 %s", local_id)
                continue
            if not reminders.apply(list_id, external_id, desired, item):
                continue
        if desired is None:
            state["seen"].pop(external_id, None)
        else:
            state["seen"][external_id] = {**desired, "updated_at": change["updated_at"]}
        state["acks"][str(local_id)] = {
            "local_id": local_id, "external_id": external_id, "updated_at": change["updated_at"],
        }
    state["pending"] = []
    save_state(path, state)


def sync_once(client, config, directory, reminders=None):
    if reminders is None:
        reminders = AppleReminders(config.reminders_list)
    list_id, items = reminders.snapshot()
    scope = sha256(f"{config.server_url}\n{list_id}".encode()).hexdigest()[:20]
    path = directory / f"reminders-{scope}.json"
    state = load_state(path, {"seen": {}, "items": {}, "acks": {}, "pending": []})
    if state["pending"]:
        finish_pending(path, state, reminders, list_id, scope)
        return

    changes = snapshot_changes(state["seen"], items)
    for key, change in changes.items():
        if change.get("deleted"):
            state["seen"].pop(key, None)
        else:
            state["seen"][key] = change
        state["items"][key] = change
    if changes:
        save_state(path, state)

    # The server retries unacknowledged changes. Persist the reply before touching
    # Apple Reminders so restarting between a write and its ack is safe.
    while True:
        upload = list(state["items"].values())[:1000]
        acks = list(state["acks"].values())[:500]
        response = client.post(SYNC_PATH, json={"items": upload, "acknowledgements": acks})
        response.raise_for_status()
        pending = validate_response(response.json())
        for item in upload:
            del state["items"][item["external_id"]]
        for ack in acks:
            del state["acks"][str(ack["local_id"])]
        if pending or upload or acks:
            state["pending"] = pending
            save_state(path, state)
        if pending:
            finish_pending(path, state, reminders, list_id, scope)
        if not state["items"] and not state["acks"]:
            return
