import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from backend.deck_plugins import DeckPluginError, DeckPluginRegistry
from mac_agent import desktop_input


OBS = "obs-studio"
RESOLVE = "davinci-resolve"
PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


@pytest.fixture
def registry():
    registry = DeckPluginRegistry(PLUGINS, enabled=[OBS, RESOLVE])
    registry.load_enabled()
    return registry


def test_official_manifests_discover_and_load(registry):
    disabled = {entry["id"]: entry["enabled"] for entry in DeckPluginRegistry(PLUGINS).catalog()}
    assert disabled[OBS] is False
    assert disabled[RESOLVE] is False
    catalog = {entry["id"]: entry for entry in registry.catalog()}
    for plugin_id, action_count in [(OBS, 9), (RESOLVE, 14)]:
        entry = catalog[plugin_id]
        assert entry["enabled"] is True
        assert registry._plugins[plugin_id].module is not None
        assert len(entry["actions"]) == action_count
        for item in [entry, *entry["actions"], *entry["feedbacks"], *entry["variables"]]:
            assert set(item["name"]) == {"en", "zh-CN"}
            for parameter in item.get("parameters", []):
                assert set(parameter["name"]) == {"en", "zh-CN"}

    with pytest.raises(DeckPluginError, match="declared schema"):
        registry.execute_action(OBS, "switch-scene")
    with pytest.raises(DeckPluginError, match="declared schema"):
        registry.execute_action(OBS, "mute-audio", {"input_name": 23})
    with pytest.raises(DeckPluginError, match="declared schema"):
        registry.execute_action(OBS, "start-recording", {"host": "elsewhere"})


class MockOBS:
    def __init__(self, *, authenticated=False):
        self.hello = {"rpcVersion": 1}
        if authenticated:
            self.hello["authentication"] = {"salt": "salt", "challenge": "challenge"}
        self.recording = True
        self.streaming = False
        self.scene = "节目场景"
        self.rejections = {}
        self.messages = []
        self.socket = Mock(state=State.OPEN)
        self.socket.send.side_effect = lambda frame: self.messages.append(json.loads(frame))
        self.socket.recv.side_effect = self.receive

    def receive(self, *, timeout):
        if not self.messages:
            return json.dumps({"op": 0, "d": self.hello})

        message = self.messages[-1]
        opcode, data = message["op"], message["d"]
        if opcode == 1:
            return json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}})

        if opcode == 8:
            response = {"results": [self.request(request["requestType"]) for request in data["requests"]]}
        else:
            assert opcode == 6
            response = self.request(data["requestType"])
        return json.dumps({"op": opcode + 1, "d": {"requestId": data["requestId"], **response}})

    def request(self, request_type):
        code = self.rejections.get(request_type, 100)
        data = {
            "GetRecordStatus": {"outputActive": self.recording},
            "GetStreamStatus": {"outputActive": self.streaming},
            "GetCurrentProgramScene": {"currentProgramSceneName": self.scene},
            "GetSceneItemId": {"sceneItemId": 42},
        }.get(request_type)
        response = {"requestType": request_type, "requestStatus": {"result": code == 100, "code": code}}
        if data is not None and code == 100:
            response["responseData"] = data
        return response

    def requests(self):
        return [(message["d"]["requestType"], message["d"].get("requestData"))
                for message in self.messages if message["op"] == 6]


@pytest.fixture
def obs(registry, monkeypatch, tmp_path):
    module = registry._plugins[OBS].module
    config = tmp_path / "config.json"
    clock = Mock(return_value=10.0)
    connect = Mock(side_effect=AssertionError("Unexpected OBS connection"))
    monkeypatch.setattr(module, "CONFIG_PATH", config)
    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module, "monotonic", clock)
    return SimpleNamespace(config=config, clock=clock, connect=connect)


def test_obs_auth_and_all_control_requests(registry, obs):
    stub = MockOBS(authenticated=True)
    obs.config.write_text('{"host":"192.168.1.25","port":4460,"password":"obs-test-password"}', encoding="utf-8")
    obs.connect.side_effect = None
    obs.connect.return_value = stub.socket
    for action, args in [
        ("start-recording", None), ("stop-recording", None),
        ("start-streaming", None), ("stop-streaming", None),
        ("switch-scene", {"scene_name": "片头"}),
        ("mute-audio", {"input_name": "Mic/Aux"}), ("unmute-audio", {"input_name": "Mic/Aux"}),
        ("show-source", {"scene_name": "片头", "source_name": "摄像头"}),
        ("hide-source", {"source_name": "摄像头"}),
    ]:
        assert registry.execute_action(OBS, action, args) == {"ok": True}

    obs.connect.assert_called_once()
    assert obs.connect.call_args.args == ("ws://192.168.1.25:4460",)
    assert obs.connect.call_args.kwargs["open_timeout"] == 1.5
    assert stub.messages[0] == {"op": 1, "d": {
        "rpcVersion": 1, "eventSubscriptions": 0, "authentication": "L2paUmwK1c84qEzhR8STbylgb4zWX8dvqUZUrcbXQfQ=",
    }}
    assert stub.requests() == [
        ("StartRecord", None), ("StopRecord", None), ("StartStream", None), ("StopStream", None),
        ("SetCurrentProgramScene", {"sceneName": "片头"}),
        ("SetInputMute", {"inputName": "Mic/Aux", "inputMuted": True}),
        ("SetInputMute", {"inputName": "Mic/Aux", "inputMuted": False}),
        ("GetSceneItemId", {"sceneName": "片头", "sourceName": "摄像头"}),
        ("SetSceneItemEnabled", {"sceneName": "片头", "sceneItemId": 42, "sceneItemEnabled": True}),
        ("GetCurrentProgramScene", None),
        ("GetSceneItemId", {"sceneName": "节目场景", "sourceName": "摄像头"}),
        ("SetSceneItemEnabled", {"sceneName": "节目场景", "sceneItemId": 42, "sceneItemEnabled": False}),
    ]
    assert "obs-test-password" not in json.dumps(registry.catalog() + stub.messages)


def test_obs_status_refresh_and_disconnect(registry, obs):
    stub = MockOBS()
    obs.connect.side_effect = None
    obs.connect.return_value = stub.socket
    assert registry.get_state(OBS, "recording") == {"active": True, "value": "🔴"}
    assert registry.get_state(OBS, "streaming") is False
    assert registry.get_state(OBS, "current-scene") == "节目场景"
    assert registry.get_state(OBS, "connection")["active"] is True
    assert registry.get_variables(OBS) == {"recording": True, "streaming": False, "current-scene": "节目场景"}
    assert [message["op"] for message in stub.messages] == [1, 8]
    assert stub.messages[0]["d"] == {"rpcVersion": 1, "eventSubscriptions": 0}
    assert stub.messages[1]["d"]["requests"] == [
        {"requestType": name} for name in ["GetRecordStatus", "GetStreamStatus", "GetCurrentProgramScene"]
    ]
    assert obs.connect.call_args.args == ("ws://127.0.0.1:4455",)

    obs.clock.return_value += 1
    stub.recording, stub.streaming, stub.scene = False, True, "片头"
    assert registry.get_variables(OBS) == {"recording": False, "streaming": True, "current-scene": "片头"}
    assert registry.get_state(OBS, "recording") == {"active": False, "value": "○"}

    obs.clock.return_value += 1
    stub.socket.recv.side_effect = ConnectionClosedError(None, None)
    assert registry.execute_action(OBS, "stop-streaming")["ok"] is False
    assert stub.requests() == [("StopStream", None)]
    stub.socket.close.assert_called_once()
    assert registry.get_state(OBS, "recording") is None
    assert registry.get_state(OBS, "connection") == {"active": False, "value": "离线 / Offline"}
    assert registry.get_variables(OBS) == {}
    obs.connect.assert_called_once()

    obs.clock.return_value += 3
    obs.connect.return_value = MockOBS().socket
    assert registry.get_state(OBS, "connection")["active"] is True
    assert registry.get_variables(OBS)["current-scene"] == "节目场景"
    assert obs.connect.call_count == 2


def test_obs_unavailable_and_bad_config_return_friendly_results(registry, obs):
    obs.connect.side_effect = ConnectionRefusedError("internal host details")
    outcome = registry.execute_action(OBS, "start-recording")
    assert outcome["ok"] is False and "Start OBS" in outcome["message"]
    assert "internal host details" not in outcome["message"]
    assert registry.get_variables(OBS) == {}
    obs.connect.assert_called_once()

    obs.config.write_text('{"port":true,"password":"local-secret"}', encoding="utf-8")
    outcome = registry.execute_action(OBS, "start-recording")
    assert outcome["ok"] is False and "config.json" in outcome["message"]
    assert "local-secret" not in outcome["message"]
    obs.connect.assert_called_once()


def test_obs_rejected_commands_keep_connection(registry, obs):
    stub = MockOBS()
    stub.rejections = {"GetSceneItemId": 600, "StartRecord": 500, "StopStream": 501}
    obs.connect.side_effect = None
    obs.connect.return_value = stub.socket
    outcome = registry.execute_action(OBS, "hide-source", {"scene_name": "片头", "source_name": "缺失"})
    assert outcome["ok"] is False and outcome["code"] == 600
    assert stub.requests() == [("GetSceneItemId", {"sceneName": "片头", "sourceName": "缺失"})]
    assert registry.execute_action(OBS, "start-recording") == {"ok": True}
    assert registry.execute_action(OBS, "stop-streaming") == {"ok": True}
    obs.connect.assert_called_once()
    stub.socket.close.assert_not_called()


@pytest.mark.parametrize(("platform", "native", "primary"), [
    ("win32", "windows_input", "ctrl"), ("darwin", "mac_input", "cmd"),
])
def test_resolve_actions_dispatch_native_shortcuts(registry, monkeypatch, platform, native, primary):
    module = registry._plugins[RESOLVE].module
    inputs = Mock()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(desktop_input, native, inputs)
    expected = {
        "play-pause": ([], "space"),
        "shuttle-reverse": ([], "j"), "shuttle-stop": ([], "k"), "shuttle-forward": ([], "l"),
        "razor": ([], "b"), "split-clip": ([primary], "b"),
        "ripple-delete": (["shift"], "backspace"), "add-marker": ([], "m"),
        "undo": ([primary], "z"), "redo": ([primary, "shift"], "z"),
        "page-edit": (["shift"], "4"), "page-color": (["shift"], "6"),
        "page-fairlight": (["shift"], "7"), "page-deliver": (["shift"], "8"),
    }
    for action in expected:
        assert registry.execute_action(RESOLVE, action) == {"ok": True}
    assert inputs.call_args_list == [call(modifiers, key, None) for modifiers, key in expected.values()]


def test_resolve_input_failure_and_unsupported_platform(registry, monkeypatch):
    module = registry._plugins[RESOLVE].module
    platform = SimpleNamespace(platform="linux")
    dispatch = Mock(side_effect=RuntimeError("internal details"))
    monkeypatch.setattr(module, "sys", platform)
    monkeypatch.setattr(module, "dispatch_input", dispatch)
    assert registry.execute_action(RESOLVE, "play-pause")["ok"] is False
    dispatch.assert_not_called()
    platform.platform = "darwin"
    outcome = registry.execute_action(RESOLVE, "play-pause")
    assert outcome["ok"] is False and "Accessibility" in outcome["message"]
    assert "internal details" not in outcome["message"]
