import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agents import AgentRegistry, Heartbeat
from backend.config import ROOT, Settings
from backend.deck import Action, DeckStore, Step, WriteLayout, deck_router
from backend.deck_actions import DeckActions, execute_macro
from backend.deck_plugins import DeckPluginRegistry
from backend.scratchpad import Scratchpad
from mac_agent.desktop_input import dispatch_input, parse_hotkey, text_units
from mac_agent.macos import execute_command


@pytest.fixture
def deck(tmp_path):
    scratchpad = Scratchpad(tmp_path)
    scratchpad.initialize()
    store = DeckStore(scratchpad.connect)
    store.initialize()
    return store


def test_layout_revisions_and_change_stream(deck):
    async def workflow():
        stream = deck.events()
        assert await anext(stream) == "data: 0\n\n"
        original = deck.read()
        original["layout"].scenes[0].title = "Shared"
        saved = deck.write(WriteLayout(**original))
        assert saved["revision"] == 1
        await deck.publish(saved["revision"])
        assert await asyncio.wait_for(anext(stream), 0.3) == "data: 1\n\n"
        assert DeckStore(deck.connect).read()["layout"].scenes[0].title == "Shared"
        with pytest.raises(HTTPException) as error:
            deck.write(WriteLayout(**original))
        assert error.value.status_code == 409
        await stream.aclose()

    asyncio.run(workflow())


def test_catalog_config_and_example_plugin_round_trip(deck):
    settings = Settings(deck={"enabled_plugins": ["_example"], "webhooks": [
        {"id": "check", "name": "Test check", "url": "https://example.invalid/test?secret=demo", "method": "GET"},
    ]})
    plugins = DeckPluginRegistry(ROOT / "plugins", settings.deck.enabled_plugins)
    actions = DeckActions(settings, None, "windows", AgentRegistry(), None, None, plugins)
    app = FastAPI()
    app.state.media_client = None
    app.include_router(deck_router(deck, actions))
    client = TestClient(app)
    catalog = client.get("/api/deck/catalog").json()
    assert catalog["plugins"][0]["id"] == "_example"
    assert catalog["plugins"][0]["enabled"] is True
    assert catalog["webhooks"] == [{"id": "check", "name": "Test check", "method": "GET"}]
    document = client.get("/api/deck").json()
    layout = document["layout"]
    key = {"kind": "toggle", "steps": [{"action": {"id": "plugin", "params": {
        "plugin_id": "_example", "action_id": "increment", "args": {"amount": 5},
    }}}], "feedback": {"plugin_id": "_example", "feedback_id": "at-target"}}
    layout["scenes"][0]["tiles"][0] = key
    layout["scenes"][0]["tiles"][1] = {"kind": "live", "feedback": {"plugin_id": "_example", "variable_id": "count"}}
    assert client.post("/api/deck/validate", json=layout).status_code == 200
    assert client.get("/api/deck").json()["revision"] == 0
    saved = client.put("/api/deck", json=document)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert client.put("/api/deck", json=document).status_code == 409
    selection = {"revision": 1, "scene": "daily", "slot": 0}
    assert client.post("/api/deck/run", json=selection).json() == {"results": [{"value": {"count": 5}}]}
    assert client.post("/api/deck/run", json={**selection, "revision": 0}).status_code == 409
    assert client.post("/api/deck/feedback", json={"revision": 1, "scene": "daily", "slots": [0, 1]}).json() == {
        "values": {"0": True, "1": 5},
    }
    layout["scenes"][0]["tiles"][0] = {"kind": "folder", "page": "missing"}
    assert client.post("/api/deck/validate", json=layout).status_code == 422


def test_macro_orders_delays_and_stops_on_error():
    async def workflow():
        events = []

        async def invoke(action):
            events.append(action.id)
            if action.id == "text":
                raise RuntimeError("stop")

        async def delay(seconds):
            events.append(seconds)

        steps = [
            Step(action=Action(id="hotkey", params={"shortcut": "ctrl+c"})),
            Step(action=Action(id="text", params={"text": "Sample"}), delay_ms=300),
            Step(action=Action(id="focus", params={"action": "start"})),
        ]
        with pytest.raises(RuntimeError, match="stop"):
            await execute_macro(steps, invoke, delay)
        assert events == ["hotkey", 0.3, "text"]

    asyncio.run(workflow())


def test_native_shortcuts_and_snippets_dispatch_without_shell(monkeypatch):
    calls = []
    monkeypatch.setattr("mac_agent.desktop_input.windows_input", lambda *args: calls.append(("windows", *args)))
    monkeypatch.setattr("mac_agent.desktop_input.mac_input", lambda *args: calls.append(("macos", *args)))
    dispatch_input("windows", preset="desktop_right")
    execute_command("deck_keyboard", {"preset": "switch_app"})
    execute_command("deck_keyboard", {"text": "中𐐀"})
    assert calls == [
        ("windows", ["ctrl", "win"], "right", None),
        ("macos", ["cmd"], "tab", None),
        ("macos", [], None, "中𐐀"),
    ]
    assert text_units("中𐐀") == [0x4E2D, 0xD801, 0xDC00]
    for value in ("ctrl+win+cmd+d", "ctrl+unknown", "ctrl+escape+tab", "cmd+"):
        with pytest.raises(ValueError):
            parse_hotkey(value)


def test_remote_input_requires_capable_mac_and_waits_for_receipt():
    registry = AgentRegistry()
    plugins = DeckPluginRegistry(ROOT / "plugins", ())
    actions = DeckActions(Settings(), None, "windows", registry, None, None, plugins)
    now = datetime.now(timezone.utc)
    device = Heartbeat(name="Test Mac", os="macos", timestamp=now)
    registry.heartbeat("test-mac", device)
    action = Action(id="preset", params={"preset": "mission_control"})

    async def workflow():
        with pytest.raises(HTTPException) as error:
            await actions.invoke(action, "test-mac", None)
        assert error.value.status_code == 409
        registry.heartbeat("test-mac", device.model_copy(update={"capabilities": ["deck_keyboard"]}))
        task = asyncio.create_task(actions.invoke(action, "test-mac", None))
        await asyncio.sleep(0)
        command = registry.heartbeat("test-mac", device)["commands"][0]
        assert command["payload"]["preset"] == "mission_control"
        registry.result("test-mac", UUID(command["id"]), SimpleNamespace(ok=True))
        assert await asyncio.wait_for(task, 0.4) == {}

    asyncio.run(workflow())
