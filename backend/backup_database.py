"""Copy portable Vitals rows into a database built with the application's schema."""

from contextlib import closing
import sqlite3
from time import monotonic

from fastapi import HTTPException

from .ai_usage import UsageLedger
from .config import FinanceConfig, HealthConfig
from .deck import DeckStore
from .deck_plugin_manager import initialize_plugin_tables
from .expenses import ExpenseStore
from .health import HealthStore
from .life import LifeStore
from .rate_history import RateHistory
from .rates import RatePins
from .scratchpad import Scratchpad, STORAGE_KEY


# Adding a column to an application table must never implicitly make it portable.
PORTABLE_COLUMNS = {
    "scratchpad": "id kind text filename size_bytes mime_type storage_key created_at pinned",
    "scratchpad_settings": "id retention_days",
    "ai_messages": "id role content created_at",
    "ai_briefings": "date content created_at bark_attempted_at",
    "ai_token_usage": "recorded_at kind tokens",
    "media_lyrics": "cache_key payload expires_at",
    "agent_profiles": "device_id alias snapshot last_seen",
    "todos": "id title day done external_source external_id updated_at deleted reminders_synced_at",
    "habits": "id title",
    "habit_logs": "habit_id day",
    "countdowns": "id title target_date note created_at",
    "focus_sessions": "id duration_minutes completed_at day",
    "health_samples": "metric at payload priority updated_at",
    "health_file_imports": "path modified_ns size",
    "expenses": "id amount_minor currency merchant category card note source occurred_at ingest_key",
    "expense_budgets": "currency amount_minor",
    "expense_settings": "id base_currency",
    "subscriptions": "id name amount_minor currency cycle billing_anchor active",
    "subscription_reminders": "subscription_id due_date",
    "rate_pins": "id pairs",
    "rate_snapshots": "base target hour rate",
    "deck_layout": "id revision document",
}
REQUIRED_COLUMNS = {
    "scratchpad": set("id kind text filename size_bytes mime_type storage_key created_at".split()),
    "todos": {"id", "title", "day", "done"},
    "habits": {"id", "title"},
}
AUTOINCREMENT_TABLES = {"scratchpad", "ai_messages", "todos", "habits", "countdowns", "expenses", "subscriptions"}
LOCAL_PLUGIN_COLUMNS = {
    "deck_plugin_settings": "id,enabled,settings",
    "deck_plugin_management": "id,key",
}


def upload_manifest(db):
    files = {}
    for kind, key, size in db.execute("SELECT kind,storage_key,size_bytes FROM incoming.scratchpad NOT INDEXED"):
        if kind == "text" and key is None:
            continue
        if (kind != "file" or not isinstance(key, str) or not STORAGE_KEY.fullmatch(key)
                or type(size) is not int or size < 0 or key in files):
            raise ValueError("中转站记录或文件索引无效")
        files[key] = size
    return files


def copy_portable_database(source, directory, *, exporting=False):
    # Initialize an empty directory. Scratchpad startup also reconciles its uploads.
    store = Scratchpad(directory)
    store.initialize()
    health = HealthStore(store.connect, HealthConfig())
    for service in (health, LifeStore(store.connect, health.zone), ExpenseStore(store.connect, health.zone, None),
                    RatePins(store.connect, FinanceConfig()), RateHistory(None, store.connect),
                    UsageLedger(store.connect), DeckStore(store.connect)):
        service.initialize()

    with closing(sqlite3.connect(store.database.resolve().as_uri(), uri=True)) as db, db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("ATTACH DATABASE ? AS incoming", (source.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        deadline = monotonic() + 30
        if exporting:
            db.set_progress_handler(lambda: int(monotonic() > deadline), 10000)
        try:
            tables = {row[1]: row[2] for row in db.execute("PRAGMA incoming.table_list")}
            if (any(kind in {"view", "virtual", "shadow"} for kind in tables.values())
                    or db.execute("SELECT 1 FROM incoming.sqlite_schema WHERE type='trigger' LIMIT 1").fetchone()):
                raise ValueError("备份数据库包含不支持的对象")
            if REQUIRED_COLUMNS.keys() - tables.keys():
                raise ValueError("SQLite 文件缺少 Vitals 表")
            if not exporting and db.execute("PRAGMA incoming.integrity_check(1)").fetchone() != ("ok",):
                raise ValueError("SQLite 完整性校验失败")
            uploads = upload_manifest(db)
            for table, allowed in PORTABLE_COLUMNS.items():
                db.execute(f"DELETE FROM main.{table}")
                if table not in tables:
                    continue
                present = {row[1] for row in db.execute(f"PRAGMA incoming.table_info({table})")}
                if REQUIRED_COLUMNS.get(table, set()) - present:
                    raise ValueError("SQLite 文件缺少 Vitals 字段")
                columns = ",".join(name for name in allowed.split() if name in present)
                if not columns:
                    raise ValueError("SQLite 文件缺少 Vitals 字段")
                db.execute(f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM incoming.{table} NOT INDEXED")
            if "sqlite_sequence" in tables:
                for table in AUTOINCREMENT_TABLES:
                    row = db.execute("SELECT seq FROM incoming.sqlite_sequence WHERE name=?", (table,)).fetchone()
                    if row is not None and type(row[0]) is int and row[0] >= 0:
                        # Empty tables can still have IDs already issued to clients.
                        db.execute("INSERT INTO main.sqlite_sequence(name,seq) SELECT ?,? WHERE NOT EXISTS "
                                   "(SELECT 1 FROM main.sqlite_sequence WHERE name=?)", (table, row[0], table))
                        db.execute("UPDATE main.sqlite_sequence SET seq=max(seq,?) WHERE name=?", (row[0], table))
        except sqlite3.OperationalError as error:
            if exporting and monotonic() > deadline:
                raise HTTPException(503, "备份 SQLite 超时，请稍后重试") from error
            raise
    return uploads


def retain_local_plugin_settings(live_path, restored_path):
    # Only this machine's database supplies these tables, read at restart so
    # changes made after uploading a backup also survive.
    with closing(sqlite3.connect(restored_path.resolve().as_uri(), uri=True)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("ATTACH DATABASE ? AS local", (live_path.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        initialize_plugin_tables(db)
        for table, columns in LOCAL_PLUGIN_COLUMNS.items():
            if db.execute("SELECT 1 FROM local.sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone():
                db.execute(f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM local.{table}")
