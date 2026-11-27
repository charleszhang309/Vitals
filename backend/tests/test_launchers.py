import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from backend import launchers
from backend.config import Launcher
from backend.main import create_app


APP = {"id": "editor", "name": "编辑器", "kind": "app", "target": "C:/Apps/Editor.exe"}
LINK = {"id": "status", "name": "状态页", "kind": "url", "target": "https://example.test/status"}


def test_launcher_api_uses_only_saved_ids_and_hides_application_paths(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"launchers": [APP, LINK]}), encoding="utf-8")
    app = create_app(config_path=path, data_dir=tmp_path / "data", static_dir=tmp_path / "static")
    main = TestClient(app, base_url="https://vitals.tailnet.ts.net", client=("100.80.1.2", 45000))
    start = Mock()
    monkeypatch.setattr(launchers, "launch_app", start)

    response = main.get("/api/control/launchers")
    assert response.status_code == 200
    assert response.json() == {"items": [
        {"id": "editor", "name": "编辑器", "kind": "app"},
        {"id": "status", "name": "状态页", "kind": "url", "url": LINK["target"]},
    ]}
    assert response.headers["cache-control"] == "no-store"
    assert main.post("/api/control/launchers/unknown").status_code == 404
    assert main.post("/api/control/launchers/status").status_code == 409
    assert main.post("/api/control/launchers/editor", json={"target": "C:/Other.exe"}).status_code == 422
    assert main.post("/api/control/launchers/editor", headers={"Origin": "https://elsewhere.test"}).status_code == 403
    start.assert_not_called()
    assert main.post("/api/control/launchers/editor").status_code == 202
    assert start.call_args.args[0] == Launcher.model_validate(APP)
    redirect = main.get("/api/control/launchers/status/open", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == LINK["target"]
    assert main.get("/api/control/launchers/editor/open").status_code == 404


def test_only_local_setup_can_save_launchers_and_changes_apply_immediately(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"agents":{"token":"fixture-token"}}', encoding="utf-8")
    app = create_app(config_path=path, data_dir=tmp_path / "data", static_dir=tmp_path / "static")
    local = TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 45000))
    remote = TestClient(app, base_url="https://vitals.tailnet.ts.net", client=("100.80.1.2", 45000))
    assert remote.patch("/api/setup/config", json={"launchers": [APP]}).status_code == 403
    response = local.patch("/api/setup/config", json={"launchers": [APP, LINK]})
    assert response.status_code == 200
    assert response.json()["launchers"] == [APP, LINK]
    assert response.json()["restart_required"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["token"] == "fixture-token"
    assert len(remote.get("/api/control/launchers").json()["items"]) == 2
    assert local.patch("/api/setup/config", json={"launchers": [APP, APP]}).status_code == 422
    assert local.patch("/api/setup/config", json={"launchers": []}).status_code == 200
    assert remote.get("/api/control/launchers").json() == {"items": []}


@pytest.mark.parametrize(("kind", "target"), [
    ("app", "Editor.exe"),
    ("app", "C:/Apps/Editor.exe --arg"),
    ("app", r"\\server\share\Editor.exe"),
    ("app", "C:/Apps/Editor.exe:payload"),
    ("app", "/Applications/../Editor.app"),
    ("url", "javascript:alert(1)"),
    ("url", "https://user:secret@example.test"),
])
def test_invalid_launcher_targets_are_rejected(kind, target):
    with pytest.raises(ValidationError):
        Launcher.model_validate({**APP, "kind": kind, "target": target})


def test_windows_dispatch_has_a_single_executable_argument(tmp_path, monkeypatch):
    exe = tmp_path / "Example Editor.exe"
    exe.touch()
    item = Launcher.model_construct(id="editor", name="Editor", kind="app", target=str(exe))
    process = Mock()
    process.poll.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(launchers.subprocess, "Popen", popen)
    monkeypatch.setattr(launchers.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(launchers.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    launchers.launch_app(item, "windows")
    assert popen.call_args.args == ([str(exe.resolve())],)
    assert popen.call_args.kwargs["shell"] is False
    assert popen.call_args.kwargs["cwd"] == exe.parent
