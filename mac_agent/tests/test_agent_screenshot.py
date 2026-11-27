from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from mac_agent.agent import process_commands
from mac_agent.screenshot import ScreenRecordingDenied, capture_mac


def test_native_mac_capture_uses_fixed_primary_display_jpeg_arguments(monkeypatch):
    captured = []

    def run(args, **options):
        captured.extend(args)
        assert options["timeout"] == 5
        Path(args[-1]).touch()
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("mac_agent.screenshot.screen_recording_allowed", lambda: True)
    monkeypatch.setattr("mac_agent.screenshot.subprocess.run", run)
    with pytest.raises(RuntimeError, match="Mac 当前无法截图"):
        capture_mac()
    assert captured[:-1] == ["/usr/sbin/screencapture", "-x", "-m", "-t", "jpg"]
    assert Path(captured[-1]).suffix == ".jpg"
    assert not Path(captured[-1]).exists()


def test_screenshot_upload_is_ephemeral_and_requires_an_active_command(tmp_path, monkeypatch):
    command = {"id": str(uuid4()), "action": "screenshot", "payload": {}}
    route = f"/api/agents/macbook/commands/{command['id']}/screenshot"
    seen = []
    starts = iter([204, 409])

    def hub(request):
        assert request.headers["authorization"] == "Bearer token"
        seen.append(request.url.path)
        if request.url.path == route + "/start":
            return httpx.Response(next(starts))
        assert request.url.path == route
        assert request.headers["content-type"] == "image/webp"
        assert request.content == b"fixture"
        return httpx.Response(204)

    monkeypatch.setattr("mac_agent.agent.capture_mac", lambda: ("image/webp", b"fixture"))
    with httpx.Client(transport=httpx.MockTransport(hub), base_url="https://hub.example",
                      headers={"Authorization": "Bearer token"}) as client:
        process_commands(client, SimpleNamespace(device_id="macbook"), [
            {**command, "payload": {"flags": "-i"}}, command, command,
        ], tmp_path, None, None, None, None)
    assert seen == [route + "/start", route, route + "/start"]
    assert not (tmp_path / "command-receipts.json").exists()


def test_screenshot_permission_error_reports_only_the_error_code(tmp_path, monkeypatch):
    command = {"id": str(uuid4()), "action": "screenshot", "payload": {}}
    route = f"/api/agents/macbook/commands/{command['id']}/screenshot"
    seen = []

    def hub(request):
        seen.append(request.url.path)
        assert not request.content
        return httpx.Response(204)

    def denied():
        raise ScreenRecordingDenied()

    monkeypatch.setattr("mac_agent.agent.capture_mac", denied)
    with httpx.Client(transport=httpx.MockTransport(hub), base_url="https://hub.example") as client:
        process_commands(client, SimpleNamespace(device_id="macbook"), [command], tmp_path, None, None, None, None)
    assert seen == [route + "/start", route + "/error/permission"]
    assert not (tmp_path / "command-receipts.json").exists()
