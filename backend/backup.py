"""Online backup export and verified, restart-time restore."""

from contextlib import closing, contextmanager
from datetime import date
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Lock
from time import monotonic
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as CompressionError

from fastapi import APIRouter, HTTPException, Request
from PIL import Image
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import FileResponse

from .config import GIB, MIB

ARCHIVE_LIMIT = 4 * GIB
MEMBER_LIMIT = 2 * GIB
WALLPAPER_LIMIT = 10 * MIB
ENTRY_LIMIT = 50_000
ZIP_READ_LIMIT = 16 * MIB
CHUNK = 128 * 1024
KEY = re.compile(r"[0-9a-f]{32}")
UPLOAD = re.compile(r"uploads/[0-9a-f]{32}")
REQUIRED_COLUMNS = {
    "scratchpad": {"id", "kind", "text", "filename", "size_bytes", "mime_type", "storage_key", "created_at"},
    "todos": {"id", "title", "day", "done"},
    "habits": {"id", "title"},
}


def read_db(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def upload_manifest(db):
    files = {}
    for key, size in db.execute("SELECT storage_key,size_bytes FROM scratchpad WHERE kind='file'"):
        if not isinstance(key, str) or not KEY.fullmatch(key) or type(size) is not int or size < 0 or key in files:
            raise ValueError("中转站文件索引无效")
        files[key] = size
    return files


def validate_database(path):
    with closing(read_db(path)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        tables = {name: sql or "" for name, sql in db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'")}
        if (db.execute("SELECT 1 FROM sqlite_schema WHERE type IN ('trigger','view') LIMIT 1").fetchone()
                or any(sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE") for sql in tables.values())):
            raise ValueError("备份数据库包含不支持的对象")
        for table, columns in REQUIRED_COLUMNS.items():
            if table not in tables or not columns <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                raise ValueError("SQLite 文件缺少 Vitals 表或字段")
        if db.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
            raise ValueError("SQLite 完整性校验失败")
        return upload_manifest(db)


class ArchiveReader:
    def __init__(self, stream):
        self.stream = stream
        self.seek, self.tell, self.seekable = stream.seek, stream.tell, stream.seekable
        self.size = self.seek(0, os.SEEK_END)
        self.seek(0)

    def read(self, size=-1):
        # zipfile loads its central directory in one read, before member validation.
        remaining = self.size - self.tell()
        if (min(size, remaining) if size >= 0 else remaining) > ZIP_READ_LIMIT:
            raise HTTPException(413, "ZIP 目录超出大小上限")
        return self.stream.read(size)


class BackupDownload(FileResponse):
    def __init__(self, directory, path, filename):
        self.directory = directory
        super().__init__(path, filename=filename, media_type="application/zip",
                         headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.directory.cleanup()


class Backups:
    def __init__(self, store, data_dir, wallpaper_lock):
        self.store = store
        self.data_dir = data_dir
        self.work = data_dir.parent / "backups" / data_dir.name
        self.pending = self.work / "pending"
        self.wallpaper_lock = wallpaper_lock
        self.restore_lock = Lock()

    @contextmanager
    def session(self):
        # A directory swap must never race another hub using these paths.
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.work / "lock").open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt

                    if not handle.tell():
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("此数据目录已被另一个 Vitals 实例使用") from exc
            yield

    @contextmanager
    def restore_on_startup(self):
        if not self.pending.is_dir():
            yield
            return
        restore_id = f"{date.today():%Y%m%d}-{uuid4().hex}"
        previous = None
        if self.data_dir.exists():
            previous = self.work / f"previous-{restore_id}"
            self.data_dir.rename(previous)
        try:
            self.pending.rename(self.data_dir)
        except OSError:
            if previous is not None:
                previous.rename(self.data_dir)
            raise
        try:
            yield
        except BaseException:
            self.data_dir.rename(self.work / f"failed-{restore_id}")
            if previous is not None:
                previous.rename(self.data_dir)
            raise

    def export(self):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = TemporaryDirectory(prefix="export-", dir=self.work)
        root = Path(directory.name)
        try:
            target = root / "app.db"
            archive = root / "backup.zip"
            deadline = monotonic() + 30

            def limit_time(*_):
                if monotonic() > deadline:
                    raise HTTPException(503, "备份 SQLite 超时，请稍后重试")

            if self.store.database.is_symlink() or self.store.uploads.is_symlink():
                raise HTTPException(409, "备份数据库和附件目录须位于数据目录内")
            with self.store.files_lock, closing(read_db(self.store.database)) as source, closing(sqlite3.connect(target)) as copy:
                source.backup(copy, pages=128, progress=limit_time, sleep=.05)
                copy.execute("PRAGMA journal_mode=DELETE")
                uploads = upload_manifest(copy)
                with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=1) as output:
                    total = 0

                    def add(path, name, limit, expected=None):
                        nonlocal total
                        status = path.lstat()
                        if not stat.S_ISREG(status.st_mode) or (expected is not None and status.st_size != expected):
                            raise HTTPException(409, "备份源中有无效文件")
                        total += status.st_size
                        if status.st_size > limit or total >= ARCHIVE_LIMIT:
                            raise HTTPException(413, "备份超过文件大小上限")
                        output.write(path, name)

                    if len(uploads) + 2 > ENTRY_LIMIT:
                        raise HTTPException(413, "备份文件数超出上限")
                    add(target, "app.db", MEMBER_LIMIT)
                    for key, size in uploads.items():
                        add(self.store.uploads / key, f"uploads/{key}", MEMBER_LIMIT, size)
                    with self.wallpaper_lock:
                        wallpaper = self.data_dir / "wallpaper.webp"
                        if wallpaper.exists() or wallpaper.is_symlink():
                            add(wallpaper, "wallpaper.webp", WALLPAPER_LIMIT)
            if archive.stat().st_size > ARCHIVE_LIMIT:
                raise HTTPException(413, "备份包超过大小上限")
            return BackupDownload(directory, archive, f"vitals-backup-{date.today():%Y%m%d}.zip")
        except BaseException:
            directory.cleanup()
            raise

    def stage(self, stream):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.restore_lock, TemporaryDirectory(prefix="restore-", dir=self.work) as directory:
            if self.pending.exists():
                raise HTTPException(409, "已有待恢复的备份，请先重启 Vitals")
            staged = Path(directory) / "data"
            staged.mkdir(mode=0o700)
            (staged / "uploads").mkdir()
            with ZipFile(ArchiveReader(stream)) as archive:
                extract_backup(archive, staged)
            uploads = validate_database(staged / "app.db")
            if uploads != {f.name: f.stat().st_size for f in (staged / "uploads").iterdir()}:
                raise ValueError("备份中的附件与 SQLite 文件索引不一致")
            if (wallpaper := staged / "wallpaper.webp").is_file():
                with Image.open(wallpaper, formats=["WEBP"]) as image:
                    if max(image.size) > 2560 or getattr(image, "n_frames", 1) != 1:
                        raise ValueError("备份壁纸无效")
                    image.load()
            staged.rename(self.pending)


def extract_backup(archive, target):
    entries = archive.infolist()
    if len(entries) > ENTRY_LIMIT:
        raise HTTPException(413, "备份文件数超出上限")
    seen, total = set(), 0
    for entry in entries:
        name = entry.filename
        kind = stat.S_IFMT(entry.external_attr >> 16)
        directory = name == "uploads/" and kind in (0, stat.S_IFDIR)
        regular = (name in {"app.db", "wallpaper.webp"} or UPLOAD.fullmatch(name)) and kind in (0, stat.S_IFREG)
        if (name in seen or entry.orig_filename != name or not (directory or regular)
                or entry.flag_bits & 1 or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
            raise ValueError("备份包包含不允许的路径、重复项或文件类型")
        seen.add(name)
        limit = WALLPAPER_LIMIT if name == "wallpaper.webp" else MEMBER_LIMIT
        total += entry.file_size
        if entry.file_size > limit or total > ARCHIVE_LIMIT:
            raise HTTPException(413, "解压大小超过备份上限")
        if directory:
            if entry.file_size:
                raise ValueError("无效的 ZIP 目录项")
            continue
        with archive.open(entry) as incoming, (target / name).open("xb") as outgoing:
            remaining = entry.file_size
            while chunk := incoming.read(CHUNK):
                remaining -= len(chunk)
                if remaining < 0:
                    raise HTTPException(413, "解压大小与备份目录不一致")
                outgoing.write(chunk)
            if remaining:
                raise ValueError("备份成员不完整")
    if "app.db" not in seen:
        raise ValueError("备份包缺少 app.db")


class BodyTooLarge(MultiPartException):
    pass


def backup_router(backups, identity):
    router = APIRouter(prefix="/api/system", tags=["system"])

    @router.get("/info")
    def info():
        return identity

    @router.get("/backup")
    def export():
        try:
            return backups.export()
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(409, "当前数据无法生成一致的备份") from exc

    @router.post("/backup/restore", status_code=202)
    async def restore(request: Request):
        kind = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if kind not in {"application/zip", "application/octet-stream", "multipart/form-data"}:
            raise HTTPException(415, "请上传 ZIP 备份")
        limit = ARCHIVE_LIMIT + (CHUNK if kind == "multipart/form-data" else 0)
        length = request.headers.get("content-length", "")
        if length.isdecimal() and int(length) > limit:
            raise HTTPException(413, "备份包超过大小上限")

        async def body():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise BodyTooLarge("备份包超过大小上限")
                yield chunk

        try:
            if kind == "multipart/form-data":
                form = await MultiPartParser(request.headers, body(), max_files=1, max_fields=0).parse()
                try:
                    upload = form.get("file")
                    if not isinstance(upload, UploadFile) or not (upload.filename or "").lower().endswith(".zip"):
                        raise ValueError("请在 file 字段上传 .zip 备份")
                    if upload.size is None or upload.size > ARCHIVE_LIMIT:
                        raise BodyTooLarge("备份包超过大小上限")
                    await run_in_threadpool(backups.stage, upload.file)
                finally:
                    await form.close()
            else:
                with TemporaryFile() as stream:
                    async for chunk in body():
                        await run_in_threadpool(stream.write, chunk)
                    await run_in_threadpool(backups.stage, stream)
        except BodyTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except (BadZipFile, CompressionError, EOFError, MultiPartException, OSError, RuntimeError, SyntaxError,
                ValueError, sqlite3.Error, Image.DecompressionBombError) as exc:
            raise HTTPException(422, "无效的 Vitals ZIP 备份，或无法写入恢复暂存目录") from exc
        return {"restart_required": True, "message": "备份已校验，重启 Vitals 后恢复生效。"}

    return router
