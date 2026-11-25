"""将暂存箱元信息存入 SQLite，将文件保存在本地。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
from threading import Lock
from typing import Annotated, BinaryIO, Literal
from urllib.parse import quote
from uuid import uuid4

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Path as PathParam, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from .monitor import utc_now


logger = logging.getLogger(__name__)
CHUNK = 64 * 1024
PREVIEW_LIMIT = 15_000_000
STORED_FILE = re.compile(r"([0-9a-f]{32})(\.(?:part|deleting))?")
MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/x-icon"}
PUBLIC_FIELDS = "id, kind, text, filename, size_bytes, mime_type, created_at, pinned"
ItemID = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


def clean_filename(value: str) -> str:
    filename = PureWindowsPath(value).name
    filename = "".join(char for char in filename if char.isprintable())[:255].strip()
    return filename if filename and filename not in {".", ".."} else "upload"


def content_disposition(filename: str, inline: bool) -> str:
    path = PureWindowsPath(filename)
    fallback = re.sub(r"[^A-Za-z0-9_. -]", "", path.stem).strip(" .") or "download"
    if re.fullmatch(r"\.[A-Za-z0-9]+", path.suffix):
        fallback += path.suffix
    kind = "inline" if inline else "attachment"
    encoded = quote(filename, safe="")
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


class TextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20000)


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool = Field(strict=True)


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: Literal[3, 7, 30] | None


def public_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["pinned"] = bool(item["pinned"])
    return item


class UploadTooLarge(MultiPartException):
    pass


class Scratchpad:
    def __init__(self, data_dir: Path):
        self.database = data_dir / "app.db"
        self.uploads = data_dir / "uploads"
        self.files_lock = Lock()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.uploads.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS scratchpad (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('text', 'file')),
                    text TEXT,
                    filename TEXT,
                    size_bytes INTEGER,
                    mime_type TEXT,
                    storage_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))
                )
            """)
            if "pinned" not in {row["name"] for row in db.execute("PRAGMA table_info(scratchpad)")}:
                db.execute("ALTER TABLE scratchpad ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))")
            db.execute("CREATE INDEX IF NOT EXISTS scratchpad_order ON scratchpad(pinned DESC, created_at DESC, id DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_briefings (
                date TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                bark_attempted_at TEXT
            )""")
            if "bark_attempted_at" not in {row["name"] for row in db.execute("PRAGMA table_info(ai_briefings)")}:
                db.execute("ALTER TABLE ai_briefings ADD COLUMN bark_attempted_at TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS media_lyrics (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS media_lyrics_expiry ON media_lyrics(expires_at)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS scratchpad_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    retention_days INTEGER CHECK (retention_days IN (3, 7, 30))
                )
            """)
            db.execute("INSERT OR IGNORE INTO scratchpad_settings(id) VALUES (1)")
            keys = {row[0] for row in db.execute("SELECT storage_key FROM scratchpad WHERE kind = 'file'")}
        # 恢复上次中断的删除，清理操作中断留下的临时文件。
        for path in self.uploads.iterdir():
            match = STORED_FILE.fullmatch(path.name)
            if not match or not path.is_file():
                continue
            key, suffix = match.groups()
            original = self.uploads / key
            if suffix == ".deleting" and key in keys and not original.exists():
                path.replace(original)
            elif suffix is not None or key not in keys:
                self.remove_file(path)

    @staticmethod
    def remove_file(path: Path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("暂存文件清理失败，将在下次启动时重试: %s", path.name)

    def list_items(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(f"SELECT {PUBLIC_FIELDS} FROM scratchpad ORDER BY pinned DESC, created_at DESC, id DESC")
            return [public_item(row) for row in rows]

    def set_pin(self, item_id: int, pinned: bool | None) -> dict:
        with self.connect() as db:
            cursor = db.execute("UPDATE scratchpad SET pinned = COALESCE(?, 1 - pinned) WHERE id = ?", (pinned, item_id))
            if not cursor.rowcount:
                raise HTTPException(404, "中转站记录不存在")
            return public_item(db.execute(f"SELECT {PUBLIC_FIELDS} FROM scratchpad WHERE id = ?", (item_id,)).fetchone())

    def get_text(self, item_id: int) -> str:
        with self.connect() as db:
            item = db.execute("SELECT text FROM scratchpad WHERE id = ? AND kind = 'text'", (item_id,)).fetchone()
            if item is None:
                raise HTTPException(404, "中转站文本不存在")
            return item["text"]

    def retention_days(self) -> int | None:
        with self.connect() as db:
            return db.execute("SELECT retention_days FROM scratchpad_settings WHERE id = 1").fetchone()[0]

    def set_retention(self, days: int | None):
        with self.connect() as db:
            db.execute("UPDATE scratchpad_settings SET retention_days = ? WHERE id = 1", (days,))

    def add_text(self, text: str) -> dict:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO scratchpad (kind, text, created_at) VALUES ('text', ?, ?)", (text, utc_now()))
            return public_item(db.execute(f"SELECT {PUBLIC_FIELDS} FROM scratchpad WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def add_file(self, source: BinaryIO, filename: str, size: int, mime_type: str) -> dict:
        key = uuid4().hex
        temporary = self.uploads / f"{key}.part"
        final = self.uploads / key
        moved = False
        try:
            with temporary.open("xb") as target:
                shutil.copyfileobj(source, target, CHUNK)
            with self.connect() as db:
                cursor = db.execute("""
                    INSERT INTO scratchpad (kind, filename, size_bytes, mime_type, storage_key, created_at)
                    VALUES ('file', ?, ?, ?, ?, ?)
                """, (filename, size, mime_type, key, utc_now()))
                temporary.rename(final)
                moved = True
                item = public_item(db.execute(f"SELECT {PUBLIC_FIELDS} FROM scratchpad WHERE id = ?", (cursor.lastrowid,)).fetchone())
        except (OSError, sqlite3.Error):
            self.remove_file(temporary)
            if moved:
                self.remove_file(final)
            raise
        return item

    def open_file(self, item_id: int) -> tuple[sqlite3.Row, BinaryIO]:
        with self.files_lock, self.connect() as db:
            row = db.execute("SELECT * FROM scratchpad WHERE id = ? AND kind = 'file'", (item_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "中转站文件不存在")
            try:
                return row, (self.uploads / row["storage_key"]).open("rb")
            except FileNotFoundError as error:
                raise HTTPException(404, "中转站文件已缺失") from error

    def delete(self, item_id: int):
        if not self._delete(item_id):
            raise HTTPException(404, "中转站记录不存在")

    def cleanup(self, now: datetime | None = None) -> int:
        days = self.retention_days()
        if days is None:
            return 0
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="milliseconds")
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM scratchpad WHERE pinned = 0 AND created_at < ?", (cutoff,))]
        removed = 0
        for item_id in ids:
            try:
                removed += self._delete(item_id, cutoff, days)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                # Windows 上正在下载的文件留到下一轮重试。
        return removed

    def _delete(self, item_id: int, cutoff: str | None = None, days: int | None = None) -> bool:
        with self.files_lock:
            original = tombstone = None
            try:
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT storage_key, pinned, created_at FROM scratchpad WHERE id = ?", (item_id,)).fetchone()
                    if row is None:
                        return False
                    if cutoff is not None:
                        active = db.execute("SELECT retention_days FROM scratchpad_settings WHERE id = 1").fetchone()[0]
                        if active != days or row["pinned"] or row["created_at"] >= cutoff:
                            return False
                    if row["storage_key"]:
                        original = self.uploads / row["storage_key"]
                        try:
                            tombstone = original.rename(original.with_suffix(".deleting"))
                        except FileNotFoundError:
                            pass
                        except PermissionError as error:
                            raise HTTPException(409, "文件正在使用或无法移动，请稍后重试") from error
                    db.execute("DELETE FROM scratchpad WHERE id = ?", (item_id,))
            except sqlite3.Error:
                if tombstone is not None:
                    tombstone.replace(original)
                raise
            if tombstone is not None:
                self.remove_file(tombstone)
            return True


class FileStream(StreamingResponse):
    def __init__(self, store: Scratchpad, item_id: int, download: bool, preview: bool = False):
        self.store = store
        self.item_id = item_id
        self.download = download
        self.preview = preview
        self._file: BinaryIO | None = None
        self._file_lock = Lock()
        self._closed = False
        self._iterator = self._chunks()
        super().__init__(self._iterator)

    def _open(self) -> tuple[sqlite3.Row, int]:
        with self._file_lock:
            # 如果请求在工作线程取得锁之前取消，不能再打开文件。
            if self._closed:
                raise ClientDisconnect()
            row, self._file = self.store.open_file(self.item_id)
            return row, os.fstat(self._file.fileno()).st_size

    def _read(self) -> bytes:
        with self._file_lock:
            return self._file.read(CHUNK) if self._file is not None else b""

    def _is_pdf(self) -> bool:
        with self._file_lock:
            if self._file is None:
                return False
            header = self._file.read(8)
            self._file.seek(0)
            return bool(re.fullmatch(rb"%PDF-[0-9]\.[0-9]", header))

    def _close(self):
        # 即使 ASGI 任务先取消，关闭也会等待工作线程完成当前读取。
        with self._file_lock:
            self._closed = True
            if self._file is not None:
                self._file.close()
                self._file = None

    async def _close_stream(self):
        with CancelScope(shield=True):
            await run_in_threadpool(self._close)

    async def _chunks(self):
        try:
            while chunk := await run_in_threadpool(self._read):
                yield chunk
        finally:
            await self._close_stream()

    async def __call__(self, scope, receive, send):
        try:
            # 把打开操作延迟到 ASGI 开始发送响应时，避免请求提前取消留下句柄。
            row, size = await run_in_threadpool(self._open)
            filename = clean_filename(row["filename"])
            pdf = (self.preview and not self.download and size <= PREVIEW_LIMIT
                   and PureWindowsPath(filename).suffix.lower() == ".pdf" and await run_in_threadpool(self._is_pdf))
            preview = pdf or (not self.download and row["mime_type"] in PREVIEW_TYPES)
            self.headers.update({
                "Content-Type": "application/pdf" if pdf else row["mime_type"] if preview else "application/octet-stream",
                "Content-Disposition": content_disposition(filename, preview),
                "Content-Length": str(size),
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'" if pdf else "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            })
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            pass
        finally:
            try:
                await self._iterator.aclose()
            finally:
                await self._close_stream()


def scratchpad_router(store: Scratchpad, limit: int) -> APIRouter:
    router = APIRouter(prefix="/api/scratchpad", tags=["scratchpad"])

    @router.get("")
    def list_items():
        return store.list_items()

    @router.get("/settings")
    def cleanup_settings():
        return {"retention_days": store.retention_days()}

    @router.patch("/settings")
    def update_cleanup(request: RetentionRequest):
        store.set_retention(request.retention_days)
        return {"retention_days": request.retention_days}

    @router.patch("/{id}/pin")
    def pin_item(id: ItemID, request: PinRequest | None = None):
        return store.set_pin(id, request.pinned if request is not None else None)

    @router.post("/text", status_code=201)
    def create_text(request: TextRequest):
        if not request.text.strip():
            raise HTTPException(422, "请输入文字")
        return store.add_text(request.text)

    @router.post("/file", status_code=201, openapi_extra={"requestBody": {"required": True, "content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary"},
        }}},
    }}})
    async def upload_file(request: Request):
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "multipart/form-data":
            raise HTTPException(415, "请使用 multipart/form-data 上传文件")

        async def limited_stream():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                # 预留表单头开销，同时限制无 Content-Length 的上传。
                if received > limit + CHUNK:
                    raise UploadTooLarge("文件超过大小限制")
                yield chunk

        try:
            form = await MultiPartParser(request.headers, limited_stream(), max_files=1, max_fields=0).parse()
        except UploadTooLarge as error:
            raise HTTPException(413, "文件超过大小限制") from error
        except (MultiPartException, MultipartParseError) as error:
            raise HTTPException(400, "上传表单无效") from error
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or upload.size is None:
                raise HTTPException(422, "请在 file 字段上传文件")
            if upload.size > limit:
                raise HTTPException(413, "文件超过大小限制")
            filename = clean_filename(upload.filename or "")
            mime = (upload.content_type or "").partition(";")[0].strip().lower()
            if not MEDIA_TYPE.fullmatch(mime) or len(mime) > 127:
                mime = "application/octet-stream"
            return await run_in_threadpool(store.add_file, upload.file, filename, upload.size, mime)
        finally:
            await form.close()

    @router.get("/files/{id}", response_class=StreamingResponse)
    async def download_file(id: ItemID, download: bool = False, preview: bool = False):
        return FileStream(store, id, download, preview)

    @router.delete("/{id}", status_code=204)
    def delete_item(id: ItemID):
        store.delete(id)
        return Response(status_code=204)

    return router
