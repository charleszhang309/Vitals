from pathlib import Path
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.updates import APP_VERSION


REPO = "https://github.com/charleszhang309/Vitals"


def mock_client(tmp_path, handler):
    app = create_app(data_dir=tmp_path / "data", config_path=tmp_path / "config.json", static_dir=tmp_path / "static")
    app.state.external_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app, base_url="http://127.0.0.1:3993", client=("127.0.0.1", 50000))


def test_city_search_returns_only_usable_candidates(tmp_path):
    def geocode(request):
        assert request.url.host == "geocoding-api.open-meteo.com"
        assert request.url.path == "/v1/search"
        assert dict(request.url.params) == {"name": "多伦多", "count": "6", "language": "zh", "format": "json"}
        return httpx.Response(200, json={"results": [
            {"name": "多伦多", "admin1": "安大略", "country": "加拿大", "latitude": 43.7,
             "longitude": -79.4, "timezone": "America/Toronto", "population": 100},
            {"name": "Invalid", "latitude": 200, "longitude": 1, "timezone": "Invalid/Zone"},
        ]})

    client = mock_client(tmp_path, geocode)
    assert client.get("/api/setup/geocode", params={"q": " 多伦多 "}).json() == [
        {"name": "多伦多", "admin1": "安大略", "country": "加拿大", "latitude": 43.7,
         "longitude": -79.4, "timezone": "America/Toronto"},
    ]
    assert client.get("/api/setup/geocode", params={"q": " "}).status_code == 422
    assert client.get("/api/setup/geocode", params={"q": "多伦多"},
                      headers={"X-Forwarded-For": "100.80.1.2"}).status_code == 403


def test_city_search_handles_no_matches_and_timeout(tmp_path):
    def geocode(request):
        if request.url.params["name"] == "unavailable":
            raise httpx.ReadTimeout("provider timed out")
        return httpx.Response(200, json={"generationtime_ms": 0.4})

    client = mock_client(tmp_path, geocode)
    assert client.get("/api/setup/geocode", params={"q": "missing"}).json() == []
    assert client.get("/api/setup/geocode", params={"q": "unavailable"}).status_code == 502


@pytest.mark.parametrize(("platform", "asset"), [
    ("windows", "Vitals_3.0.10_x64-setup.exe"),
    ("macos", "Vitals_3.0.10_x64.dmg"),
    ("linux", None),
])
def test_update_checks_semver_selects_platform_asset_and_caches(tmp_path, monkeypatch, platform, asset):
    calls = []
    page = f"{REPO}/releases/tag/v3.0.10"
    base = f"{REPO}/releases/download/v3.0.10"

    def release(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"tag_name": "v3.0.10", "html_url": page, "body": "Update notes", "assets": [
            {"name": "external.exe", "browser_download_url": "https://example.invalid/download.exe"},
            {"name": "Vitals_3.0.10_arm64-setup.exe", "browser_download_url": f"{base}/Vitals_3.0.10_arm64-setup.exe"},
            {"name": "Vitals_3.0.10_x64-setup.exe", "browser_download_url": f"{base}/Vitals_3.0.10_x64-setup.exe"},
            {"name": "Vitals_3.0.10_x64.dmg", "browser_download_url": f"{base}/Vitals_3.0.10_x64.dmg"},
        ]})

    monkeypatch.setattr("backend.main.host_identity", lambda: {"platform": platform, "name": "test"})
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    client = mock_client(tmp_path, release)
    assert client.get("/api/system/version").json() == {"version": APP_VERSION, "build_id": None, "platform": platform, "desktop": False}
    for _ in range(2):
        assert client.get("/api/system/update/check").json() == {
            "current_version": APP_VERSION, "latest_version": "3.0.10", "has_update": True,
            "release_url": page, "notes": "Update notes", "download_url": f"{base}/{asset}" if asset else page,
        }
    assert calls == ["https://api.github.com/repos/charleszhang309/Vitals/releases/latest"]


@pytest.mark.parametrize("tag", [f"v{APP_VERSION}+build.5", f"v{APP_VERSION}-rc.10", "v1.99.99"])
def test_update_does_not_offer_equal_or_older_versions(tmp_path, tag):
    client = mock_client(tmp_path, lambda _: httpx.Response(200, json={"tag_name": tag, "assets": []}))
    assert client.get("/api/system/update/check").json()["has_update"] is False


def test_update_distinguishes_no_release_from_github_failure(tmp_path):
    no_release = mock_client(tmp_path, lambda _: httpx.Response(404))
    result = no_release.get("/api/system/update/check")
    assert result.status_code == 200
    assert result.json()["latest_version"] is None
    unavailable = mock_client(tmp_path, lambda _: httpx.Response(429))
    assert unavailable.get("/api/system/update/check").status_code == 502


def test_version_matches_frontend_and_desktop_metadata():
    root = Path(__file__).resolve().parents[2]
    for file in ("frontend/package.json", "desktop/src-tauri/tauri.conf.json"):
        assert json.loads((root / file).read_text(encoding="utf-8"))["version"] == APP_VERSION


def test_version_detects_a_new_frontend_without_a_backend_restart(tmp_path):
    client = mock_client(tmp_path, lambda _: httpx.Response(404))
    index = tmp_path / "static" / "index.html"
    assert client.get("/api/system/version").json()["build_id"] is None
    index.parent.mkdir()
    index.write_text("<script src='/assets/first.js'></script>", encoding="utf-8")
    first = client.get("/api/system/version")
    assert first.headers["cache-control"] == "no-store"
    assert client.get("/api/system/version").json()["build_id"] == first.json()["build_id"]
    index.write_text("<script src='/assets/second.js'></script>", encoding="utf-8")
    second = client.get("/api/system/version").json()
    assert second["version"] == first.json()["version"]
    assert second["build_id"] and second["build_id"] != first.json()["build_id"]
