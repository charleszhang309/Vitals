"""验证壁纸的存储、格式和上传限制。"""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from backend.config import Settings
from backend.main import create_app


def png() -> bytes:
    with BytesIO() as out:
        with Image.new("RGB", (16, 12), (80, 120, 170)) as image:
            image.save(out, format="PNG")
        return out.getvalue()


def client(directory: Path) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(), directory)), base_url="http://localhost",
    )


class WallpaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_is_shared_and_persists_then_can_be_reset(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            async with client(path) as first:
                self.assertEqual((await first.get("/api/wallpaper")).json(), {"version": None})
                result = await first.put("/api/wallpaper", content=png(), headers={"Content-Type": "image/png"})
                self.assertEqual(result.status_code, 200)
                version = result.json()["version"]
                self.assertTrue(version)
            async with client(path) as second:
                self.assertEqual((await second.get("/api/wallpaper")).json()["version"], version)
                picture = await second.get("/api/wallpaper/image")
                self.assertEqual(picture.headers["content-type"], "image/webp")
                with Image.open(BytesIO(picture.content)) as decoded:
                    self.assertEqual(decoded.size, (16, 12))
                self.assertEqual((await second.delete("/api/wallpaper")).status_code, 204)
                self.assertEqual((await second.get("/api/wallpaper/image")).status_code, 404)
                self.assertFalse((path / "wallpaper.webp").exists())

    async def test_invalid_or_large_uploads_preserve_wallpaper(self):
        with TemporaryDirectory() as folder:
            async with client(Path(folder)) as session:
                initial = await session.put("/api/wallpaper", content=png())
                spoofed = await session.put("/api/wallpaper", content=b"<svg></svg>", headers={"Content-Type": "image/png"})
                self.assertEqual(spoofed.status_code, 415)
                with patch("backend.wallpaper.MAX_INPUT", 8):
                    self.assertEqual((await session.put("/api/wallpaper", content=b"x" * 9)).status_code, 413)

                    async def chunks():
                        yield b"12345"
                        yield b"67890"

                    self.assertEqual((await session.put("/api/wallpaper", content=chunks())).status_code, 413)
                remote = await session.delete("/api/wallpaper", headers={"Origin": "http://unrelated.test"})
                self.assertEqual(remote.status_code, 403)
                self.assertEqual((await session.get("/api/wallpaper")).json(), initial.json())
