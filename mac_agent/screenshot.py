"""Capture and compress one snapshot of the active desktop."""

import ctypes
from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, features


MAX_WIDTH = 1440
MAX_HEIGHT = 4096
MAX_BYTES = 2 * 1024 * 1024
SCREEN_RECORDING_HELP = "请给 Vitals 授予录屏权限，然后重启 Vitals。"


class ScreenRecordingDenied(RuntimeError):
    pass


def compress_screen(image):
    image.thumbnail((MAX_WIDTH, MAX_HEIGHT))
    # Start from fresh pixel data so the output never carries source metadata.
    with image.convert("RGB") as rgb, Image.new("RGB", rgb.size) as clean, BytesIO() as target:
        clean.paste(rgb)
        if features.check("webp"):
            clean.save(target, "WEBP", quality=80)
            if target.tell() <= MAX_BYTES:
                return "image/webp", target.getvalue()
        for quality in (82, 65, 48):
            target.seek(0)
            target.truncate()
            clean.save(target, "JPEG", quality=quality, optimize=True)
            if target.tell() <= MAX_BYTES:
                return "image/jpeg", target.getvalue()
    raise RuntimeError("截图超过大小限制")


def screen_recording_allowed():
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    preflight = getattr(graphics, "CGPreflightScreenCaptureAccess", None)
    if preflight is None:
        return True
    preflight.argtypes = ()
    preflight.restype = ctypes.c_bool
    return preflight()


def capture_mac():
    if not screen_recording_allowed():
        raise ScreenRecordingDenied(SCREEN_RECORDING_HELP)
    with TemporaryDirectory(prefix="vitals-screenshot-") as directory:
        path = Path(directory) / "screen.jpg"
        capture = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-m", "-t", "jpg", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        if capture.returncode:
            raise RuntimeError("Mac 当前无法截图，请在设备上登录并解锁桌面。")
        with path.open("rb") as source:
            path.unlink()
            with Image.open(source, formats=["JPEG"]) as image:
                return compress_screen(image)
