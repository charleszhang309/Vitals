import asyncio
from io import BytesIO
import json
import stat
import warnings
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import pytest

from backend import deck_plugin_archive as archives


MANIFEST = json.dumps({"id": "counter", "name": {"en": "Counter"}, "version": "1.0.0"}).encode()
SOURCE = b"def execute_action(action_id, params):\n    return params\n"
ARCHIVE_URL = "https://github.com/example/counter/archive/refs/heads/main.zip"
RELEASE_URL = "https://github.com/example/counter/releases/latest/download/counter.zip"


def make_archive(entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.comment = b"GitHub can include the commit SHA here"
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def plugin_entries(prefix=""):
    return [(f"{prefix}manifest.json", MANIFEST), (f"{prefix}plugin.py", SOURCE)]


@pytest.mark.parametrize("prefix", ["", "counter-main/", "repo-main/counter/"])
def test_extracts_one_plugin_and_its_support_files(prefix):
    package = archives.extract_package(make_archive([
        *plugin_entries(prefix), (f"{prefix}support/README.md", b"Settings help"),
    ]))
    assert package.id == "counter"
    assert package.files == {
        "manifest.json": MANIFEST, "plugin.py": SOURCE, "support/README.md": b"Settings help",
    }


@pytest.mark.parametrize("entry", [
    "counter-main/../outside.txt", "counter-main/a\\asset.txt", "counter-main/C:/asset.txt",
    "counter-main/plugin.py:alternate", "/absolute/asset.txt", "counter-main//asset.txt",
    "counter-main/COM1.txt", "counter-main/a./asset.txt", "counter-main/Plugin.py",
    "counter-main/Support/a.txt", "counter-main/support", "extra-root/asset.txt",
])
def test_rejects_unsafe_paths_and_conflicting_roots(entry):
    if "\\" in entry:
        # ZipInfo normalizes separators on Windows during construction.
        raw_entry = ZipInfo(entry)
        raw_entry.filename = entry
        entry = raw_entry
    archive = make_archive([
        *plugin_entries("counter-main/"), ("counter-main/support/item.txt", b"help"), (entry, b""),
    ])
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(archive)


@pytest.mark.parametrize("attributes", [stat.S_IFLNK << 16, stat.S_IFIFO << 16, 0x0400])
def test_rejects_special_entries(attributes):
    special = ZipInfo("counter-main/support.txt")
    special.create_system = 3
    special.external_attr = attributes
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(make_archive([*plugin_entries("counter-main/"), (special, b"")]))


def test_rejects_duplicate_zip_entries():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        archive = make_archive([*plugin_entries(), ("plugin.py", SOURCE)])
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(archive)


def test_rejects_archive_with_two_plugins():
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(make_archive([
            *plugin_entries("repo-main/first/"), *plugin_entries("repo-main/second/"),
        ]))


def test_rejects_manifest_with_duplicate_members():
    duplicate = MANIFEST.replace(b'"id":', b'"id": "other", "id":')
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(make_archive([("manifest.json", duplicate), ("plugin.py", SOURCE)]))


def test_rejects_windows_device_id_even_when_the_archive_folder_is_safe():
    device = MANIFEST.replace(b'"counter"', b'"con"')
    with pytest.raises(archives.PluginArchiveError, match="manifest is invalid"):
        archives.extract_package(make_archive([
            ("counter-main/manifest.json", device), ("counter-main/plugin.py", SOURCE),
        ]))


def test_rejects_oversized_uncompressed_archive(monkeypatch):
    monkeypatch.setattr(archives, "MAX_UNCOMPRESSED_BYTES", 1024)
    archive = make_archive([*plugin_entries(), ("support.txt", b"a" * 1024)])
    with pytest.raises(archives.PluginArchiveError):
        archives.extract_package(archive)


def mock_download(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        archives.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize(("source", "location"), [
    (ARCHIVE_URL, "https://codeload.github.com/example/counter/zip/refs/heads/main"),
    (RELEASE_URL, "https://release-assets.githubusercontent.com/github-production-release-asset/1/sample?sig=token"),
])
def test_follows_github_redirect_without_sending_cookies(monkeypatch, source, location):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": location, "Set-Cookie": "private=1; Domain=github.com"})
        return httpx.Response(200, stream=httpx.ByteStream(b"ZIP bytes"))

    mock_download(monkeypatch, respond)
    assert asyncio.run(archives.download_github_archive(source)) == b"ZIP bytes"
    assert [str(request.url) for request in calls] == [source, location]
    assert all("cookie" not in request.headers for request in calls)


@pytest.mark.parametrize("url", [
    "http://github.com/example/counter/archive/main.zip", "https://github.com/example/counter",
    "https://github.com/example/counter/issues/1", "https://github.com.example/archive/main.zip",
    "https://github.com@not-github.example/example/counter/archive/main.zip",
    "https://github.com/example/counter/archive/dir/%2e%2e/main.zip",
    "https://github.com/example/counter/archive/main.zip?secret=abcd",
])
def test_rejects_non_archive_download_urls_before_connecting(monkeypatch, url):
    mock_download(monkeypatch, lambda request: pytest.fail("Unexpected HTTP request"))
    with pytest.raises(archives.PluginArchiveError):
        asyncio.run(archives.download_github_archive(url))


def test_rejects_offsite_redirect_without_exposing_location(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.test/?private=token"})

    mock_download(monkeypatch, respond)
    with pytest.raises(archives.PluginArchiveError) as error:
        asyncio.run(archives.download_github_archive(ARCHIVE_URL))
    assert len(calls) == 1
    assert "token" not in str(error.value)


def test_stops_stream_without_a_content_length_at_byte_limit(monkeypatch):
    monkeypatch.setattr(archives, "MAX_ARCHIVE_BYTES", 8)
    mock_download(monkeypatch, lambda request: httpx.Response(200, stream=httpx.ByteStream(b"large content")))
    with pytest.raises(archives.PluginArchiveError):
        asyncio.run(archives.download_github_archive(ARCHIVE_URL))


def test_stops_download_when_headers_exceed_the_total_time_limit(monkeypatch):
    monkeypatch.setattr(archives, "TOTAL_TIMEOUT_SECONDS", 0.02)

    async def stalled_response(request):
        await asyncio.sleep(2)
        pytest.fail("Download did not enforce its time limit")

    mock_download(monkeypatch, stalled_response)
    with pytest.raises(archives.PluginArchiveError):
        asyncio.run(archives.download_github_archive(ARCHIVE_URL))
