from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.deck_plugin_manager import DeckPluginManager, deck_plugin_router
from backend.deck_plugins import DeckPluginError, DeckPluginRegistry
from backend.main import PrivateNetwork, http_origin
from backend.scratchpad import Scratchpad
from backend.setup_api import direct_setup_request


OFFICIAL = Path(__file__).resolve().parents[2] / "plugins"
BASE = "/api/deck/plugins"
KEY = "/api/setup/deck/plugins/key"


@pytest.fixture
def store(tmp_path):
    scratchpad = Scratchpad(tmp_path / "data")
    scratchpad.initialize()
    return scratchpad


def make_manager(store, legacy=None):
    registry = DeckPluginRegistry(legacy or store.database.parent / "legacy")
    manager = DeckPluginManager(registry, store.database.parent, OFFICIAL, store.connect)
    manager.initialize()
    return manager


def clients(manager):
    app = FastAPI()
    app.add_middleware(PrivateNetwork)
    app.include_router(deck_plugin_router(
        manager, lambda request: direct_setup_request(request.scope, request.headers, http_origin),
    ))
    return (
        TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 50001)),
        TestClient(app, base_url="https://demo.example.ts.net", client=("100.64.1.2", 50002)),
    )


def test_official_index_and_remote_admin_authorization(store):
    manager = make_manager(store)
    local, remote = clients(manager)
    inventory = remote.get(BASE)
    assert inventory.headers["cache-control"] == "no-store"
    assert not inventory.json()["can_manage"]
    assert inventory.json()["installed"] == []
    assert {item["id"] for item in inventory.json()["catalog"]} == {"obs-studio", "davinci-resolve"}
    assert remote.get(KEY).status_code == 403
    assert local.get(KEY, headers={"X-Forwarded-For": "100.64.1.2"}).status_code == 403
    assert remote.post(BASE + "/install", json={"plugin_id": "obs-studio"}).status_code == 403

    header = {"X-Vitals-Deck-Key": local.get(KEY).json()["key"]}
    assert remote.get(BASE, headers=header).json()["can_manage"]
    assert remote.post(BASE + "/install", json={"plugin_id": "obs-studio"},
                       headers={**header, "Origin": "https://outsider.test"}).status_code == 403
    assert remote.post(BASE + "/install", json={"plugin_id": "obs-studio"}, headers=header).status_code == 201

    listing = remote.get(BASE).json()
    plugin, = listing["installed"]
    assert plugin["id"] == "obs-studio" and plugin["enabled"] and plugin["managed"]
    assert plugin["config"] == {"host": "127.0.0.1", "port": 4455}
    assert plugin["configured_secrets"] == []
    assert next(item for item in listing["catalog"] if item["id"] == "obs-studio")["installed"]


def test_obs_configuration_is_live_persistent_and_redacted(store):
    manager = make_manager(store)
    local, remote = clients(manager)
    assert local.post(BASE + "/install", json={"plugin_id": "obs-studio"}).status_code == 201
    route = BASE + "/obs-studio/config"
    secret = "mock-obs-password"
    saved = local.put(route, json={"config": {"host": "localhost", "port": 4456, "password": secret}})
    assert saved.status_code == 200
    assert secret not in saved.text
    assert secret not in remote.get(BASE).text
    assert secret not in json.dumps(manager.registry.catalog())
    assert saved.json()["installed"][0]["configured_secrets"] == ["password"]
    module = manager.registry.find("obs-studio").module
    assert module._connection_settings() == ("ws://localhost:4456", secret)
    assert local.put(route, json={"config": {"port": 70000, "password": "rejected"}}).status_code == 422
    assert local.put(route, json={"config": {"host": "https://invalid.test"}}).status_code == 422
    assert manager.registry.find("obs-studio").module._connection_settings() == ("ws://localhost:4456", secret)
    assert local.put(route, json={"config": {"password": "", "port": 4457}}).status_code == 200
    restarted = make_manager(store)
    assert restarted.registry.find("obs-studio").module._connection_settings() == ("ws://localhost:4457", secret)


def test_disable_enable_and_uninstall_refresh_runtime_and_disk(store):
    manager = make_manager(store)
    local, _ = clients(manager)
    assert local.post(BASE + "/install", json={"plugin_id": "davinci-resolve"}).status_code == 201
    plugin_id = "davinci-resolve"
    base = f"{BASE}/{plugin_id}"
    assert local.post(base + "/toggle", json={"enabled": False}).json()["installed"][0]["enabled"] is False
    assert manager.registry.find(plugin_id).module is None
    assert make_manager(store).registry.find(plugin_id).enabled is False
    with pytest.raises(DeckPluginError, match="disabled"):
        manager.registry.execute_action(plugin_id, "undo")
    assert local.post(base + "/toggle", json={"enabled": True}).status_code == 200
    assert manager.registry.find(plugin_id).module is not None
    assert local.delete(base).json()["installed"] == []
    assert not (manager.root / plugin_id).exists()
    assert make_manager(store).inventory(True)["installed"] == []


def test_legacy_plugin_keeps_default_config_and_is_not_uninstalled(store, tmp_path):
    legacy = tmp_path / "legacy-plugins"
    directory = legacy / "obs-studio"
    directory.mkdir(parents=True)
    for name in ("manifest.json", "plugin.py"):
        (directory / name).write_bytes((OFFICIAL / "obs-studio" / name).read_bytes())
    (directory / "config.json").write_text(json.dumps({"port": 4456, "password": "mock-obsolete"}), encoding="utf-8")
    manager = make_manager(store, legacy)
    local, _ = clients(manager)
    plugin, = local.get(BASE).json()["installed"]
    assert not plugin["managed"] and plugin["config"]["port"] == 4456
    assert plugin["configured_secrets"] == ["password"]
    assert local.delete(BASE + "/obs-studio").status_code == 409
    assert (directory / "plugin.py").is_file()


def test_custom_install_requires_trust_and_rejects_invalid_archives(store, monkeypatch):
    manager = make_manager(store)
    local, _ = clients(manager)
    url = "https://github.com/vitals-test/example/archive/refs/heads/main.zip"
    archive = BytesIO()
    with ZipFile(archive, "w") as packed:
        packed.writestr("example-main/manifest.json", json.dumps({
            "id": "example", "name": {"en": "Example"}, "version": "1",
            "actions": [{"id": "ping", "name": {"en": "Ping"}}],
        }))
        packed.writestr("example-main/plugin.py", "def execute_action(action, params):\n    return 'pong'\n")
    requests = []

    async def download(value):
        requests.append(value)
        return archive.getvalue() if len(requests) > 1 else b"invalid zip"

    monkeypatch.setattr("backend.deck_plugin_manager.download_github_archive", download)
    assert local.post(BASE + "/install", json={"url": url}).status_code == 422
    assert not requests
    assert local.post(BASE + "/install", json={"url": url, "trust": True}).status_code == 422
    assert manager.registry.inventory() == ()
    assert local.post(BASE + "/install", json={"url": url, "trust": True}).status_code == 201
    assert requests == [url, url]
    assert manager.registry.execute_action("example", "ping") == "pong"
