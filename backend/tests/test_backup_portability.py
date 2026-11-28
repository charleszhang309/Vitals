from contextlib import closing
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sqlite3
import stat
from threading import Lock
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi import HTTPException
from PIL import Image
import pytest

from backend.backup import Backups
from backend.deck_plugin_manager import initialize_plugin_tables
from backend.scratchpad import Scratchpad


def fixture_hub(root):
    store = Scratchpad(root)
    store.initialize()
    with store.connect() as db:
        # A legacy schema with no reminder columns or Deck tables.
        db.execute("CREATE TABLE todos(id INTEGER PRIMARY KEY, title TEXT, day TEXT, done INTEGER)")
        db.execute("CREATE TABLE habits(id INTEGER PRIMARY KEY, title TEXT)")
        initialize_plugin_tables(db)
        db.execute("INSERT INTO deck_plugin_management VALUES (1,'fixture-manager-key')")
        db.execute("""INSERT INTO deck_plugin_settings VALUES ('fixture',1,'{"password":"fixture-password"}')""")
    return Backups(store, root, Lock())


def archive_bytes(database, members=None):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("app.db", database.read_bytes())
        for name, data in (members or {}).items():
            archive.writestr(name, data)
    output.seek(0)
    return output


def test_export_copies_only_portable_columns_and_valid_icons(tmp_path):
    backups = fixture_hub(tmp_path / "source")
    with backups.store.connect() as db:
        db.execute("ALTER TABLE habits ADD COLUMN private_setting TEXT")
        db.execute("INSERT INTO habits VALUES(5,'walk','fixture-private-column')")
        db.execute("CREATE TABLE unrelated (value)")
        db.execute("INSERT INTO unrelated VALUES('fixture-unlisted-table')")
    image = BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="PNG")
    data = image.getvalue()
    name = sha256(data).hexdigest() + ".png"
    icons = backups.data_dir / "deck_icons"
    icons.mkdir()
    (icons / name).write_bytes(data)
    (icons / ".icon-upload.tmp").write_bytes(b"fixture")
    response = backups.export()
    try:
        with ZipFile(response.path) as archive:
            assert set(archive.namelist()) == {"app.db", "deck_icons/" + name}
            database = archive.read("app.db")
            assert all(value not in database for value in (
                b"fixture-manager-key", b"fixture-password", b"fixture-private-column", b"fixture-unlisted-table",
            ))
            exported = tmp_path / "exported.db"
            exported.write_bytes(database)
            with closing(sqlite3.connect(exported)) as db:
                assert db.execute("SELECT * FROM habits").fetchall() == [(5, "walk")]
                assert not db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'deck_plugin_%'").fetchall()
        target = Backups(Scratchpad(tmp_path / "fresh"), tmp_path / "fresh", Lock())
        with Path(response.path).open("rb") as stream:
            target.stage(stream)
        with target.restore_on_startup():
            assert (target.data_dir / "deck_icons" / name).read_bytes() == data
            with target.store.connect() as db:
                initialize_plugin_tables(db)
                assert not db.execute("SELECT * FROM deck_plugin_management").fetchall()
    finally:
        response.directory.cleanup()


def test_legacy_restore_preserves_destination_plugin_state_at_restart(tmp_path):
    source = fixture_hub(tmp_path / "source")
    target = fixture_hub(tmp_path / "destination")
    source.store.add_text("legacy note")
    code = target.data_dir / "plugins" / "fixture" / "plugin.py"
    code.parent.mkdir(parents=True)
    code.write_bytes(b"# synthetic local plugin")
    target.stage(archive_bytes(source.store.database))
    with target.store.connect() as db:
        db.execute("UPDATE deck_plugin_management SET key='destination-key'")
        db.execute("""UPDATE deck_plugin_settings SET enabled=0,settings='{"password":"destination-only"}'""")
    with target.restore_on_startup():
        assert code.read_bytes() == b"# synthetic local plugin"
        assert target.store.list_items()[0]["text"] == "legacy note"
        with target.store.connect() as db:
            assert db.execute("SELECT key FROM deck_plugin_management").fetchone()[0] == "destination-key"
            assert tuple(db.execute("SELECT enabled,settings FROM deck_plugin_settings").fetchone()) == (
                0, '{"password":"destination-only"}',
            )


@pytest.mark.parametrize("kind,key", [("text", "a" * 32), ("file", "../outside"), ("file", None), ("unknown", None)])
def test_restore_checks_every_scratchpad_record(tmp_path, kind, key):
    hub = fixture_hub(tmp_path / "data")
    with hub.store.connect() as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO scratchpad(kind,storage_key,size_bytes,created_at) VALUES(?,?,0,'now')", (kind, key))
    with pytest.raises(ValueError):
        hub.stage(archive_bytes(hub.store.database))
    assert not hub.pending.exists()


@pytest.mark.parametrize("name,data", [
    ("deck_icons/" + "a" * 64 + ".svg", b"<svg/>"),
    ("deck_icons/" + sha256(b"<html/>").hexdigest() + ".svg", b"<html/>"),
    ("deck_icons/icon.svg", b"<svg/>"),
    ("plugins/fixture/plugin.py", b"# package"),
])
def test_invalid_archive_members_never_stage(tmp_path, name, data):
    hub = fixture_hub(tmp_path / "data")
    with pytest.raises(ValueError):
        hub.stage(archive_bytes(hub.store.database, {name: data}))
    assert not hub.pending.exists()


def test_invalid_attachment_paths_never_read_or_delete(tmp_path, monkeypatch):
    hub = fixture_hub(tmp_path / "data")
    item = hub.store.add_file(BytesIO(b"item"), "file", 4, "text/plain")["id"]
    outside = hub.data_dir / "outside"
    outside.write_bytes(b"keep")
    with hub.store.connect() as db:
        key = db.execute("SELECT storage_key FROM scratchpad WHERE id=?", (item,)).fetchone()[0]
        db.execute("UPDATE scratchpad SET storage_key='../outside' WHERE id=?", (item,))
    for action in (hub.store.open_file, hub.store.delete):
        with pytest.raises(HTTPException) as error:
            action(item)
        assert error.value.status_code == 409
    assert outside.read_bytes() == b"keep"

    with hub.store.connect() as db:
        db.execute("UPDATE scratchpad SET storage_key=? WHERE id=?", (key, item))
    original = Path.lstat
    for flagged in (hub.store.uploads, hub.store.uploads / key):
        def reparse(path):
            status = original(path)
            if path == flagged:
                return SimpleNamespace(st_mode=status.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return status

        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", reparse)
            for action in (hub.store.open_file, hub.store.delete):
                with pytest.raises(HTTPException):
                    action(item)
