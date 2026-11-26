import asyncio
from contextlib import asynccontextmanager, closing
from io import BytesIO
import sqlite3
from zipfile import ZipFile

import httpx
import pytest

from backend.config import Settings
from backend.life import LifeStore
from backend.main import create_app


@asynccontextmanager
async def live_client(data, config_path=None):
    app = create_app(Settings(), data, config_path=config_path)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        yield client


def make_zip(**entries):
    arc = BytesIO()
    with ZipFile(arc, "w") as file:
        for name, content in entries.items():
            file.writestr(name, content)
    return arc.getvalue()


def test_backup_online_round_trip_applies_on_restart(tmp_path):
    async def verify():
        data = tmp_path / "data"
        config = tmp_path / "config.json"
        config.write_text('{"agents":{"token":"not-in-backup"}}', encoding="utf-8")
        async with live_client(data, config) as api:
            note = (await api.post("/api/scratchpad/text", json={"text": "before backup"})).raise_for_status().json()
            blob = (await api.post("/api/scratchpad/file", files={"file": ("sample.bin", b"payload")})).raise_for_status().json()
            (data / "config.json").write_text("secret", encoding="utf-8")
            response = await api.get("/api/system/backup")
            assert response.status_code == 200
            assert "vitals-backup-" in response.headers["Content-Disposition"]
            with ZipFile(BytesIO(response.content)) as file:
                assert len(file.namelist()) == 2
                assert "app.db" in file.namelist()
                assert not any("config" in name for name in file.namelist())
            assert (await api.delete(f"/api/scratchpad/{note['id']}")).status_code == 204
            staged = await api.post(
                "/api/system/backup/restore", files={"file": ("backup.zip", response.content, "application/zip")}
            )
            assert staged.status_code == 202 and staged.json()["restart_required"]
            assert all(row["id"] != note["id"] for row in (await api.get("/api/scratchpad")).json())

        async with live_client(data, config) as api:
            assert len((await api.get("/api/scratchpad")).json()) == 2
            assert (await api.get(f"/api/scratchpad/files/{blob['id']}")).content == b"payload"
        assert "not-in-backup" in config.read_text()

    asyncio.run(verify())


def test_restore_rejects_unsafe_requests_and_preserves_live_database(tmp_path, monkeypatch):
    foreign_db = tmp_path / "foreign.db"
    with closing(sqlite3.connect(foreign_db)) as db:
        db.execute("CREATE TABLE foreign_app (value TEXT)")
    traversal = make_zip(**{"app.db": b"not a database", "../config.json": b"changed"})
    foreign = make_zip(**{"app.db": foreign_db.read_bytes()})
    headers = {"Content-Type": "application/zip"}

    async def verify():
        async with live_client(tmp_path / "data") as api:
            assert (await api.post("/api/scratchpad/text", json={"text": "keep"})).status_code == 201
            external = {"Origin": "https://outside.example"}
            assert (await api.get("/api/system/backup", headers=external)).status_code == 403
            for body in (traversal, foreign):
                assert (await api.post("/api/system/backup/restore", content=body, headers=headers)).status_code == 422
            assert (await api.post("/api/system/backup/restore", content=foreign, headers=headers | external)).status_code == 403

            monkeypatch.setattr("backend.backup.ARCHIVE_LIMIT", 64)

            async def streamed():
                yield foreign[:55]
                yield foreign[55:]

            assert (await api.post("/api/system/backup/restore", content=streamed(), headers=headers)).status_code == 413
            assert (await api.get("/api/scratchpad")).json()[0]["text"] == "keep"

    asyncio.run(verify())


def test_restore_keeps_previous_database_when_initialization_fails(tmp_path, monkeypatch):
    async def verify():
        data = tmp_path / "data"
        async with live_client(data) as api:
            archive = (await api.get("/api/system/backup")).raise_for_status().content
            (await api.post("/api/scratchpad/text", json={"text": "keep"})).raise_for_status()
            result = await api.post("/api/system/backup/restore", content=archive, headers={"Content-Type": "application/zip"})
            assert result.status_code == 202

        def bad_migration(self):
            raise sqlite3.DatabaseError("migration failed")

        with monkeypatch.context() as patch:
            patch.setattr(LifeStore, "initialize", bad_migration)
            with pytest.raises(sqlite3.DatabaseError, match="migration failed"):
                async with live_client(data):
                    pytest.fail("failed migration unexpectedly served requests")
        async with live_client(data) as api:
            assert (await api.get("/api/scratchpad")).json()[0]["text"] == "keep"
        assert len(list((tmp_path / "backups/data").glob("failed-*"))) == 1

    asyncio.run(verify())


def test_macos_desktop_routes_confirm_sleep_and_dispatch_native_commands(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("backend.main.host_identity", lambda: {"platform": "macos", "name": "Test Mac"})
    monkeypatch.setattr("mac_agent.macos.execute_command",
                        lambda command, _, **options: commands.append((command, options["caffeinate"])))

    async def verify():
        async with live_client(tmp_path / "data") as api:
            assert (await api.get("/api/system/info")).json() == {"platform": "macos", "name": "Test Mac"}
            assert (await api.get("/api/control/power")).json()["modes"] == []
            assert (await api.post("/api/control/desktop", json={"action": "sleep"})).status_code == 409
            for action in ("display-off", "sleep", "caffeinate-toggle"):
                result = await api.post("/api/control/desktop", json={"action": action, "confirmed": True})
                assert result.status_code == 200 and result.json()["accepted"]

    asyncio.run(verify())
    assert [name for name, _ in commands] == ["display_lock", "sleep", "caffeinate_toggle"]
    assert commands[0][1] is commands[1][1] is commands[2][1]
