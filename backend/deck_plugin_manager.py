"""Install trusted Deck packages and persist their host-side settings."""

import asyncio
import json
import logging
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as RoutePath, Request, Response

from .deck_plugin_archive import PluginArchiveError, PluginPackage, download_github_archive, extract_package
from .deck_plugins import DeckPluginError, PLUGIN_ID_PATTERN, _read_manifest, _verified_path


OFFICIAL = ("obs-studio", "davinci-resolve")
MAX_REQUEST = 128 * 1024
PluginId = Annotated[str, RoutePath(pattern=PLUGIN_ID_PATTERN)]
logger = logging.getLogger(__name__)


def initialize_plugin_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS deck_plugin_settings (
        id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, settings TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS deck_plugin_management (
        id INTEGER PRIMARY KEY CHECK(id=1), key TEXT NOT NULL
    )""")


def settings_from(plugin, supplied, *, stored=False):
    declared = {field.id: field for field in plugin.manifest.config_schema}
    if not stored and supplied.keys() - declared.keys():
        raise HTTPException(422, "插件配置字段无效")
    output = {}
    for identifier, value in supplied.items():
        field = declared.get(identifier)
        if field is not None and field.accepts(value):
            output[identifier] = value
        elif not stored:
            raise HTTPException(422, "插件配置值无效")
    return output


def legacy_settings(plugin):
    file = plugin.directory / "config.json"
    if not plugin.manifest.config_schema:
        return {}
    try:
        if not _verified_path(file) or not file.is_file():
            return {}
        with file.open("rb") as stream:
            raw = stream.read(MAX_REQUEST + 1)
        data = json.loads(raw.decode("utf-8-sig")) if len(raw) <= MAX_REQUEST else None
    except (OSError, ValueError, RuntimeError):
        return {}
    return settings_from(plugin, data, stored=True) if isinstance(data, dict) else {}


class DeckPluginManager:
    def __init__(self, registry, data_dir, official_dir, connect):
        self.registry = registry
        self.root = Path(data_dir).resolve() / "plugins"
        self.official_dir = official_dir
        self.connect = connect
        self.lock = RLock()

    def initialize(self):
        with self.connect() as db:
            initialize_plugin_tables(db)
            saved = {row[0]: (bool(row[1]), json.loads(row[2]))
                     for row in db.execute("SELECT id,enabled,settings FROM deck_plugin_settings")}
        for plugin in self.registry.inventory():
            plugin_id = plugin.manifest.id
            enabled, settings = saved.get(plugin_id, (plugin.enabled, {}))
            effective = {**legacy_settings(plugin), **settings_from(plugin, settings, stored=True)}
            self.registry.update(plugin_id, enabled, effective, activate=False)
        if self.root.exists() and _verified_path(self.root) and self.root.is_dir():
            for directory in self.root.iterdir():
                try:
                    manifest = _read_manifest(directory)
                    enabled, settings = saved.get(manifest.id, (False, {}))
                    plugin = self.registry.register(directory, enabled, {}, activate=False)
                    self.registry.update(manifest.id, enabled, settings_from(plugin, settings, stored=True), activate=False)
                except (OSError, ValueError, RuntimeError):
                    logger.warning("Skipping unavailable managed Deck plugin")
        self.registry.load_enabled()

    def key(self, *, issue=False):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT key FROM deck_plugin_management WHERE id=1").fetchone()
            if row:
                return row[0]
            if issue:
                value = token_urlsafe(32)
                db.execute("INSERT INTO deck_plugin_management (id,key) VALUES (1,?)", (value,))
                return value
        return None

    def authorized(self, supplied):
        if not supplied or not supplied.isascii() or len(supplied) > 128:
            return False
        saved = self.key()
        return bool(saved and compare_digest(saved, supplied))

    def installed(self, plugin_id):
        try:
            return self.registry.find(plugin_id)
        except DeckPluginError:
            raise HTTPException(404, "找不到已安装的 Deck 插件") from None

    def official_manifest(self, plugin_id):
        if plugin_id in OFFICIAL:
            try:
                return _read_manifest(self.official_dir / plugin_id)
            except (OSError, RuntimeError, ValueError):
                logger.warning("Official Deck package unavailable (%s)", plugin_id)
        raise HTTPException(404, "未找到官方 Deck 插件")

    def inventory(self, can_manage):
        with self.lock:
            installed = []
            for plugin in self.registry.inventory():
                manifest = plugin.manifest
                public = {field.id: plugin.settings.get(field.id, field.default) for field in manifest.config_schema if not field.secret}
                installed.append({
                    "id": manifest.id, "name": manifest.name, "version": manifest.version, "enabled": plugin.enabled,
                    "managed": plugin.directory.parent == self.root, "config": public,
                    "config_schema": [field.model_dump(exclude_none=True) for field in manifest.config_schema],
                    "configured_secrets": [field.id for field in manifest.config_schema if field.secret and plugin.settings.get(field.id)],
                })
            ids = {plugin["id"] for plugin in installed}
            catalog = []
            for plugin_id in OFFICIAL:
                try:
                    manifest = self.official_manifest(plugin_id)
                except HTTPException:
                    continue
                catalog.append({"id": plugin_id, "name": manifest.name, "version": manifest.version, "installed": plugin_id in ids})
            return {"installed": installed, "catalog": catalog, "can_manage": can_manage}

    def official_package(self, plugin_id):
        manifest = self.official_manifest(plugin_id)
        path = self.official_dir / manifest.id
        try:
            files = {name: (path / name).read_bytes() for name in ("plugin.py", "manifest.json")}
        except OSError:
            raise HTTPException(404, "未找到官方 Deck 插件") from None
        return PluginPackage(manifest.id, files)

    def write(self, plugin_id, enabled, settings, db):
        db.execute("""INSERT INTO deck_plugin_settings VALUES(?,?,?)
                      ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, settings=excluded.settings""",
                   (plugin_id, enabled, json.dumps(settings, ensure_ascii=False, allow_nan=False)))

    def update(self, plugin_id, *, enabled=None, updates=None):
        with self.lock:
            plugin = self.installed(plugin_id)
            previous = plugin.enabled, plugin.settings.copy()
            settings = previous[1].copy()
            if updates is not None:
                secret = {field.id for field in plugin.manifest.config_schema if field.secret}
                settings.update(settings_from(plugin, {
                    key: value for key, value in updates.items() if key not in secret or value != ""
                }))
            enabled = previous[0] if enabled is None else enabled
            try:
                self.registry.update(plugin_id, enabled, settings)
                with self.connect() as db:
                    self.write(plugin_id, enabled, settings, db)
            except Exception as error:
                try:
                    self.registry.update(plugin_id, *previous)
                except DeckPluginError:
                    logger.warning("Could not restore Deck plugin runtime (%s)", plugin_id)
                if isinstance(error, DeckPluginError):
                    raise HTTPException(422, "Deck 插件无法加载或应用配置") from None
                raise

    def install(self, package):
        with self.lock:
            try:
                self.registry.find(package.id)
            except DeckPluginError:
                pass
            else:
                raise HTTPException(409, "该 Deck 插件已安装")
            self.root.mkdir(exist_ok=True)
            if not _verified_path(self.root) or not self.root.is_dir():
                raise HTTPException(409, "Deck 插件目录不可用")
            destination = self.root / package.id
            if destination.exists() or destination.is_symlink():
                raise HTTPException(409, "该 Deck 插件已安装")
            with TemporaryDirectory(prefix=".install-", dir=self.root) as staging:
                directory = Path(staging) / package.id
                directory.mkdir()
                for filename, content in package.files.items():
                    member = directory / filename
                    member.parent.mkdir(parents=True, exist_ok=True)
                    member.write_bytes(content)
                _read_manifest(directory)
                directory.rename(destination)
                registered = False
                try:
                    self.registry.register(destination, True, {})
                    registered = True
                    with self.connect() as db:
                        self.write(package.id, True, {}, db)
                except Exception as error:
                    if registered:
                        self.registry.remove(package.id)
                    destination.rename(directory)
                    if isinstance(error, DeckPluginError):
                        raise HTTPException(422, "Deck 插件无法加载或应用配置") from None
                    raise

    def remove(self, plugin_id):
        with self.lock:
            plugin = self.installed(plugin_id)
            if plugin.directory.parent != self.root or not _verified_path(plugin.directory):
                raise HTTPException(409, "只能卸载在插件商店安装的插件")
            with TemporaryDirectory(prefix=".remove-", dir=self.root) as staging:
                parked = Path(staging) / plugin_id
                enabled = plugin.enabled
                self.registry.remove(plugin_id)
                try:
                    plugin.directory.rename(parked)
                    with self.connect() as db:
                        db.execute("DELETE FROM deck_plugin_settings WHERE id=?", (plugin_id,))
                except Exception:
                    if parked.exists():
                        parked.rename(plugin.directory)
                    self.registry.register(plugin.directory, enabled, plugin.settings)
                    raise


async def request_object(request):
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > MAX_REQUEST:
            raise HTTPException(413, "插件请求过大")
        body.extend(part)
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        raise HTTPException(422, "插件请求无效")
    return value


def deck_plugin_router(manager, local_request):
    router = APIRouter(tags=["deck plugins"])
    base = "/api/deck/plugins"
    install_slot = asyncio.Lock()

    def can_manage(request):
        return local_request(request) or manager.authorized(request.headers.get("x-vitals-deck-key"))

    async def require_management(request):
        if not await asyncio.to_thread(can_manage, request):
            raise HTTPException(403, "需要 Deck 插件管理密钥")

    @router.get("/api/setup/deck/plugins/key")
    def key(request: Request, response: Response):
        if not local_request(request):
            raise HTTPException(403, "仅可在主机本机查看管理密钥")
        response.headers["Cache-Control"] = "no-store"
        return {"key": manager.key(issue=True)}

    @router.get(base)
    def inventory(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return manager.inventory(can_manage(request))

    @router.post(base + "/{plugin_id}/toggle")
    async def toggle(plugin_id: PluginId, request: Request):
        await require_management(request)
        body = await request_object(request)
        if body.keys() != {"enabled"} or type(body["enabled"]) is not bool:
            raise HTTPException(422, "插件请求无效")
        await asyncio.to_thread(manager.update, plugin_id, enabled=body["enabled"])
        return await asyncio.to_thread(manager.inventory, True)

    @router.put(base + "/{plugin_id}/config")
    async def save_config(plugin_id: PluginId, request: Request):
        await require_management(request)
        body = await request_object(request)
        if body.keys() != {"config"} or not isinstance(body["config"], dict):
            raise HTTPException(422, "插件请求无效")
        await asyncio.to_thread(manager.update, plugin_id, updates=body["config"])
        return await asyncio.to_thread(manager.inventory, True)

    @router.post(base + "/install", status_code=201)
    async def install(request: Request):
        await require_management(request)
        body = await request_object(request)
        if install_slot.locked():
            raise HTTPException(409, "正在安装另一个 Deck 插件")
        async with install_slot:
            try:
                if body.keys() == {"plugin_id"} and isinstance(body["plugin_id"], str):
                    package = await asyncio.to_thread(manager.official_package, body["plugin_id"])
                elif body.keys() == {"url", "trust"} and isinstance(body["url"], str) and body["trust"] is True:
                    package = await asyncio.to_thread(extract_package, await download_github_archive(body["url"]))
                    if package.id in OFFICIAL:
                        raise HTTPException(409, "请从官方索引安装此插件")
                else:
                    raise HTTPException(422, "请选择官方插件，或确认信任 GitHub ZIP")
                await asyncio.to_thread(manager.install, package)
            except PluginArchiveError:
                raise HTTPException(422, "GitHub ZIP 无法下载或未通过插件校验") from None
        return await asyncio.to_thread(manager.inventory, True)

    @router.delete(base + "/{plugin_id}")
    async def remove(plugin_id: PluginId, request: Request):
        await require_management(request)
        await asyncio.to_thread(manager.remove, plugin_id)
        return await asyncio.to_thread(manager.inventory, True)

    return router
