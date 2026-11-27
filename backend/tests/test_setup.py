import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import setup_api
from backend.main import create_app


@pytest.mark.parametrize(("peer", "headers", "expected"), [
    ("127.0.0.1", {"host": "127.0.0.1:3993"}, 200),
    ("::1", {"host": "[::1]:3993"}, 200),
    ("100.80.1.2", {"host": "127.0.0.1:3993"}, 403),
    ("127.0.0.1", {"host": "vitals.example.ts.net"}, 403),
    ("127.0.0.1", {"host": "127.0.0.1:3993", "x-forwarded-for": "100.80.1.2"}, 403),
    ("127.0.0.1", {"host": "127.0.0.1:3993", "tailscale-user-login": "example@example.com"}, 403),
    ("127.0.0.1", {"host": "127.0.0.1:3993", "origin": "https://other.example"}, 403),
])
def test_setup_requires_direct_loopback(tmp_path, peer, headers, expected):
    app = create_app(data_dir=tmp_path / "data", config_path=tmp_path / "config.json")
    client = TestClient(app, client=(peer, 42000))
    assert client.get("/api/setup/config", headers=headers).status_code == expected
    if expected == 403:
        assert client.post("/api/setup/agents/token", headers=headers, json={"regenerate": False}).status_code == 403
        assert client.post("/api/setup/tailscale/serve", headers=headers, json={"enabled": False}).status_code == 403


def setup_client(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    app = create_app(data_dir=tmp_path / "data", config_path=path)
    return TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 50000)), path


def test_config_patch_keeps_existing_secrets_and_rejects_partial_writes(tmp_path):
    client, path = setup_client(tmp_path, {
        "ai": {"api_key": "private-ai", "enabled": True},
        "agents": {"token": "private-agent"},
        "health": {"ingest_token": "private-health"},
        "webhook": {"kind": "bark", "bark_url_template": "https://api.day.app/private-bark/{title}/{body}"},
        "calendar": {"sources": [
            {"id": "family", "type": "icloud", "name": "Family",
             "apple_id": "private-apple", "app_password": "private-password"},
            {"id": "feed", "type": "ics", "name": "Feed", "url": "webcal://example.com/private-feed"},
        ]},
    })
    response = client.patch("/api/setup/config", json={
        "weather": {"city_name": "Montréal"},
        "ai": {"model": "gpt-test", "api_key": "  "},
        "webhook": {"device_key": "", "daily_briefing_time": "10:00"},
        "calendar": {"sources": [
            {"id": "family", "type": "icloud", "name": "Renamed", "apple_id": "", "app_password": None},
            {"id": "feed", "type": "ics", "name": "Feed", "url": "", "writable": True},
        ]},
    })
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert response.json()["ai"]["has_api_key"] is True
    assert response.json()["webhook"]["has_key"] is True
    assert response.json()["calendar"]["sources"][0]["has_app_password"] is True
    assert response.json()["calendar"]["sources"][1]["writable"] is False
    assert response.headers["cache-control"] == "no-store"
    assert "private-" not in response.text
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["weather"]["city_name"] == "Montréal"
    assert saved["ai"] == {"api_key": "private-ai", "enabled": True, "model": "gpt-test"}
    assert saved["webhook"]["bark_url_template"] == "https://api.day.app/private-bark/{title}/{body}"
    assert saved["calendar"]["sources"][0]["app_password"] == "private-password"
    assert saved["calendar"]["sources"][1]["url"] == "webcal://example.com/private-feed"
    assert saved["health"]["ingest_token"] == "private-health"
    assert saved["agents"]["token"] == "private-agent"
    original = path.read_bytes()
    bad = client.patch("/api/setup/config", json={"weather": {"latitude": 1000}, "ai": {"api_key": "private-new"}})
    assert bad.status_code == 422
    assert "private-new" not in bad.text
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".config-*.tmp"))


def test_setup_pairing_token_can_be_read_and_rotated_locally(tmp_path):
    client, path = setup_client(tmp_path, {"agents": {"token": "old-token"}})
    assert "old-token" not in client.get("/api/setup/config").text
    before = client.post("/api/setup/agents/token", json={"regenerate": False})
    assert before.json() == {"token": "old-token", "restart_required": False}
    rotated = client.post("/api/setup/agents/token", json={"regenerate": True})
    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    content = rotated.json()
    assert content["token"] != "old-token" and len(content["token"]) >= 32
    assert content["restart_required"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["token"] == content["token"]


@pytest.mark.parametrize("installed", [
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
    None,
])
def test_tailscale_discovery_on_macos_with_a_minimal_path(monkeypatch, installed):
    monkeypatch.setattr(setup_api, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(Path, "is_file", lambda path: path.as_posix() == installed)
    monkeypatch.setattr(setup_api.os, "access", lambda path, mode: str(path) == installed)
    monkeypatch.setattr(setup_api.shutil, "which", lambda _: None)
    assert setup_api.tailscale_cli() == installed


def test_tailscale_writes_only_the_vitals_https_rule(tmp_path, monkeypatch):
    client, _ = setup_client(tmp_path, {})
    commands = []
    handlers = {"/other": {"Proxy": "http://127.0.0.1:8000"}}
    funnel = {}

    def tailcli(exe, *args):
        if args == ("status", "--json"):
            data = {"BackendState": "Running", "Self": {"Online": True, "DNSName": "vitals.example.ts.net."}}
        elif args == ("serve", "status", "--json"):
            data = {"TCP": {"443": {"HTTPS": True}},
                    "Web": {"vitals.example.ts.net:443": {"Handlers": handlers}}, "AllowFunnel": funnel}
        else:
            commands.append(args)
            if args == ("serve", "--bg", "3993"):
                handlers["/"] = {"Proxy": "http://127.0.0.1:3993"}
            if args == ("serve", "--bg", "--https=443", "off"):
                del handlers["/"]
            data = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))

    monkeypatch.setattr("backend.setup_api.tailscale_cli", lambda: "tailscale")
    monkeypatch.setattr("backend.setup_api.run_tailscale", tailcli)
    path = "/api/setup/tailscale/serve"
    assert client.post(path, json={"enabled": True}).json()["https_url"] == "https://vitals.example.ts.net/"
    assert client.post(path, json={"enabled": False}).json()["serve_enabled"] is False
    assert handlers == {"/other": {"Proxy": "http://127.0.0.1:8000"}}
    handlers["/"] = {"Proxy": "http://127.0.0.1:8001"}
    assert client.post(path, json={"enabled": True}).status_code == 409
    del handlers["/"]
    funnel["vitals.example.ts.net:443"] = True
    assert client.post(path, json={"enabled": True}).status_code == 409
    assert commands == [("serve", "--bg", "3993"), ("serve", "--bg", "--https=443", "off")]


def test_windows_desktop_hub_uses_its_own_audio_session(tmp_path, monkeypatch):
    from backend.monitor import host_identity
    from backend.media_desktop import WindowsAudio

    def volume(self, change=None):
        return {"available": True, "percent": 40 if change is None else change.percent, "muted": False}

    monkeypatch.setattr("backend.main.host_identity", lambda: {**host_identity(), "platform": "windows"})
    monkeypatch.setattr("backend.media_desktop.media_apartment", nullcontext)
    monkeypatch.setattr(WindowsAudio, "__init__", lambda self: None)
    monkeypatch.setattr(WindowsAudio, "volume", volume)
    app = create_app(data_dir=tmp_path / "data", config_path=tmp_path / "config.json", interactive_media=True)
    with TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 52000)) as client:
        assert client.get("/api/media/volume").json()["percent"] == 40
        assert client.post("/api/media/volume", json={"percent": 55}).json()["percent"] == 55
