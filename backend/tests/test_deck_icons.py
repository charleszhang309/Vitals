from hashlib import sha256
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from backend.deck import Tile
from backend.deck_icons import deck_icons_router


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z" fill="currentColor"/></svg>'


@pytest.mark.parametrize("icon", ["lucide:circle-check", "/api/deck/icons/" + "c" * 64 + ".webp", "🧑‍💻", "ABC", ""])
def test_deck_tiles_keep_supported_icon_values(icon):
    assert Tile(kind="live", live="cpu", icon=icon).icon == icon


@pytest.mark.parametrize("icon", [
    "lucide:Bad", "lucide:two--parts", "https://a.co", "//a.co/img", "data:image/png",
    "/api/deck/icons/" + "c" * 64 + ".gif", "/api/deck/icons/" + "c" * 64 + ".png?extra=1",
    "/api/deck/icons/../deck.db", "a" * 17,
])
def test_deck_tiles_reject_invalid_icon_references(icon):
    with pytest.raises(ValidationError):
        Tile(kind="live", live="cpu", icon=icon)


@pytest.fixture
def icons(tmp_path):
    app = FastAPI()
    app.include_router(deck_icons_router(tmp_path))
    return TestClient(app), tmp_path / "deck_icons"


@pytest.mark.parametrize(("image_format", "original_name", "extension", "media_type"), [
    ("PNG", "user-image.PNG", "png", "image/png"),
    ("JPEG", "user-image.jpeg", "jpg", "image/jpeg"),
    ("WEBP", "user-image.webp", "webp", "image/webp"),
])
def test_upload_stores_and_serves_content_addressed_raster(icons, image_format, original_name, extension, media_type):
    client, storage = icons
    output = BytesIO()
    Image.new("RGB", (2, 2), (10, 30, 20)).save(output, format=image_format)
    image = output.getvalue()
    post = client.post("/api/deck/icons", files={"file": (original_name, image, "application/octet-stream")})
    assert post.status_code == 201
    name = f"{sha256(image).hexdigest()}.{extension}"
    assert post.json() == {"id": name, "url": f"/api/deck/icons/{name}"}
    assert client.post("/api/deck/icons", files={"file": ("another." + extension, image)}).json() == post.json()
    assert list(storage.iterdir()) == [storage / name]
    get = client.get(post.json()["url"])
    assert get.status_code == 200
    assert get.content == image
    assert get.headers["Content-Type"] == media_type
    assert get.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert get.headers["X-Content-Type-Options"] == "nosniff"


def test_upload_safely_persists_svg(icons):
    client, storage = icons
    upload = client.post("/api/deck/icons", files={"file": ("icon.svg", b"<!--comment-->" + SVG)})
    assert upload.status_code == 201
    data = upload.json()
    result = client.get(data["url"])
    assert result.status_code == 200
    assert result.headers["Content-Type"] == "image/svg+xml"
    assert "sandbox" in result.headers["Content-Security-Policy"]
    assert "default-src 'none'" in result.headers["Content-Security-Policy"]
    assert b"viewBox" in result.content and b"M2 2h20v20H2z" in result.content
    assert b"<!--" not in result.content
    assert data["id"] == f"{sha256(result.content).hexdigest()}.svg"
    assert client.post("/api/deck/icons", files={"file": ("icon.svg", SVG)}).json() == data
    assert list(storage.iterdir()) == [storage / data["id"]]


@pytest.mark.parametrize(("filename", "payload"), [
    ("fake.png", SVG),
    ("fake.svg", b"<html/>"),
    ("fake.bmp", b"BM"),
    ("corrupt.png", b"\x89PNG\r\n\x1a\n"),
    ("bad.svg", b"<svg><script/></svg>"),
    ("bad.svg", b"<svg><foreignObject/></svg>"),
    ("bad.svg", b'<svg><image href="remote"/></svg>'),
    ("bad.svg", b'<svg xmlns:xlink="http://www.w3.org/1999/xlink"><g xlink:href="remote"/></svg>'),
    ("bad.svg", b'<svg><path onload="handler"/></svg>'),
    ("bad.svg", b'<svg><path style="fill: red"/></svg>'),
    ("bad.svg", b'<svg><path fill="url(https://example.invalid/paint)"/></svg>'),
    ("bad.svg", b'<!DOCTYPE svg><svg/>'),
    ("bad.svg", b'<?xml-stylesheet href="remote"?><svg/>'),
    ("bad.svg", b"<svg>" + b"<g>" * 100 + b"</g>" * 100 + b"</svg>"),
])
def test_upload_rejects_unsupported_and_active_image_content(icons, filename, payload):
    client, storage = icons
    result = client.post("/api/deck/icons", files={"file": (filename, payload, "image/png")})
    assert result.status_code == 415
    assert not storage.exists()


def test_upload_limits_data_and_rejects_missing_files(icons):
    client, storage = icons
    limit = 512 * 1024
    valid = SVG + b" " * (limit - len(SVG))
    assert client.post("/api/deck/icons", files={"file": ("icon.svg", valid)}).status_code == 201
    assert client.post("/api/deck/icons", files={"file": ("too-large.svg", valid + b" ")}).status_code == 413
    assert client.post("/api/deck/icons").status_code == 422
    assert client.get("/api/deck/icons/" + "0" * 64 + ".png").status_code == 404
    assert client.get("/api/deck/icons/%2e%2e%2fdeck.db").status_code == 404
    assert len(list(storage.iterdir())) == 1


def test_upload_bounds_multipart_body_without_a_content_length(icons):
    client, storage = icons
    response = client.post(
        "/api/deck/icons",
        headers={"Content-Type": "multipart/form-data; boundary=icon"},
        content=iter([
            b'--icon\r\nContent-Disposition: form-data; name="file"; filename="icon.svg"\r\n\r\n',
            SVG,
            b" " * (600 * 1024),
            b"\r\n--icon--\r\n",
        ]),
    )
    assert response.status_code == 413
    assert not storage.exists()
