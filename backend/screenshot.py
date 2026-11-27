"""Serve a single screenshot to the caller that requested it."""

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import subprocess
from threading import Lock
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from PIL import Image, ImageGrab

from mac_agent.screenshot import (
    MAX_BYTES, MAX_HEIGHT, MAX_WIDTH, SCREEN_RECORDING_HELP, ScreenRecordingDenied, capture_mac, compress_screen,
)

from .agents import DeviceId, ScreenshotCommand


CAPTURE_TIMEOUT = 12
UPLOAD_TIMEOUT = 5
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
FORMATS = {"image/webp": "WEBP", "image/jpeg": "JPEG"}
MAC_CAPTURE_HELP = "Mac 当前无法截图，请在设备上登录并解锁桌面，然后重试。"
WINDOWS_CAPTURE_HELP = "Windows 当前无法截图，请在登录的桌面会话中运行 Vitals 并解锁屏幕。"


def screenshot_error(status, detail):
    return HTTPException(status, detail, headers=HEADERS)


@dataclass
class PendingScreenshot:
    device_id: str
    command_id: UUID
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False
    uploading: bool = False
    image: tuple[str, bytes] | None = None
    error: str | None = None


async def disconnected(request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def wait_for_capture(operation, cancellation, timeout):
    work = asyncio.create_task(operation)
    stop = asyncio.create_task(cancellation)
    try:
        ready, _ = await asyncio.wait({work, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if work in ready:
            return work.result()
        if stop in ready:
            stop.result()
            raise screenshot_error(410, "截图请求已取消")
        raise screenshot_error(504, "截图等待超时，请重试")
    finally:
        work.cancel()
        stop.cancel()
        await asyncio.gather(work, stop, return_exceptions=True)


def valid_image(contents, format_name):
    try:
        with Image.open(BytesIO(contents), formats=[format_name]) as image:
            if image.width > MAX_WIDTH or image.height > MAX_HEIGHT or getattr(image, "is_animated", False):
                return False
            image.load()
            return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


async def read_upload(request, format_name):
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise screenshot_error(400, "截图长度无效")
        if int(length) > MAX_BYTES:
            raise screenshot_error(413, "截图超过大小限制")
    contents = bytearray()
    async for chunk in request.stream():
        if len(contents) + len(chunk) > MAX_BYTES:
            raise screenshot_error(413, "截图超过大小限制")
        contents.extend(chunk)
    if not await asyncio.to_thread(valid_image, contents, format_name):
        raise screenshot_error(422, "截图格式或尺寸无效")
    return bytes(contents)


def screenshot_router(registry, authenticate, platform):
    router = APIRouter(tags=["screenshot"])
    capture_slot = asyncio.Lock()
    local_capture_lock = Lock()
    pending = None

    def local_capture():
        # ImageGrab may still be returning in its worker after the caller leaves.
        if not local_capture_lock.acquire(blocking=False):
            raise screenshot_error(409, "正在截图，请稍候")
        try:
            if platform == "windows":
                with ImageGrab.grab(all_screens=False) as image:
                    return compress_screen(image)
            if platform == "macos":
                return capture_mac()
            raise screenshot_error(501, "此平台不支持截图")
        except ScreenRecordingDenied:
            raise screenshot_error(403, SCREEN_RECORDING_HELP) from None
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, Image.DecompressionBombError):
            help_text = MAC_CAPTURE_HELP if platform == "macos" else WINDOWS_CAPTURE_HELP
            raise screenshot_error(503, help_text) from None
        finally:
            local_capture_lock.release()

    def active_request(device_id, cmd_id):
        transfer = pending
        if (transfer is None or transfer.device_id != device_id or transfer.command_id != cmd_id
                or transfer.ready.is_set()):
            raise screenshot_error(404, "截图请求已失效")
        try:
            registry.require_screenshot_delivery(device_id, cmd_id)
        except HTTPException as error:
            raise screenshot_error(error.status_code, error.detail) from None
        return transfer

    @router.post("/api/control/screenshot", response_class=Response)
    async def local(request: Request):
        if capture_slot.locked():
            raise screenshot_error(409, "正在截图，请稍候")
        async with capture_slot:
            media_type, contents = await wait_for_capture(
                asyncio.to_thread(local_capture), disconnected(request), CAPTURE_TIMEOUT,
            )
        return Response(contents, media_type=media_type, headers=HEADERS)

    @router.post("/api/control/screenshot/{device_id}", response_class=Response)
    async def remote(device_id: DeviceId, request: Request):
        nonlocal pending
        if capture_slot.locked():
            raise screenshot_error(409, "正在截图，请稍候")
        async with capture_slot:
            try:
                command = registry.enqueue(device_id, ScreenshotCommand())
            except HTTPException as error:
                raise screenshot_error(error.status_code, error.detail) from None
            transfer = pending = PendingScreenshot(device_id, UUID(command["id"]))
            try:
                await wait_for_capture(transfer.ready.wait(), disconnected(request), CAPTURE_TIMEOUT)
                if transfer.error:
                    denied = transfer.error == "permission"
                    raise screenshot_error(403 if denied else 503, SCREEN_RECORDING_HELP if denied else MAC_CAPTURE_HELP)
                media_type, contents = transfer.image
                return Response(contents, media_type=media_type, headers=HEADERS)
            finally:
                pending = None
                transfer.cancelled.set()
                transfer.image = None
                registry.discard_screenshot(device_id, transfer.command_id)

    receipt = "/api/agents/{device_id}/commands/{cmd_id}/screenshot"

    @router.post(receipt + "/start", status_code=204, dependencies=[Depends(authenticate)])
    async def start(device_id: DeviceId, cmd_id: UUID):
        transfer = active_request(device_id, cmd_id)
        if transfer.started:
            raise screenshot_error(409, "设备已开始截图")
        transfer.started = True
        return Response(status_code=204, headers=HEADERS)

    @router.post(receipt, status_code=204, dependencies=[Depends(authenticate)])
    async def upload(device_id: DeviceId, cmd_id: UUID, request: Request):
        transfer = active_request(device_id, cmd_id)
        if not transfer.started or transfer.uploading:
            raise screenshot_error(409, "截图请求状态无效")
        media_type = request.headers.get("content-type", "").lower()
        format_name = FORMATS.get(media_type)
        if not format_name:
            raise screenshot_error(415, "截图必须使用 WebP 或 JPEG")
        transfer.uploading = True
        try:
            contents = await wait_for_capture(read_upload(request, format_name), transfer.cancelled.wait(), UPLOAD_TIMEOUT)
            active_request(device_id, cmd_id)
            transfer.image = media_type, contents
            transfer.ready.set()
        finally:
            transfer.uploading = False
        return Response(status_code=204, headers=HEADERS)

    @router.post(receipt + "/error/{code}", status_code=204, dependencies=[Depends(authenticate)])
    async def report_error(device_id: DeviceId, cmd_id: UUID, code: Literal["permission", "capture"]):
        transfer = active_request(device_id, cmd_id)
        if not transfer.started:
            raise screenshot_error(409, "截图请求状态无效")
        transfer.error = code
        transfer.ready.set()
        return Response(status_code=204, headers=HEADERS)

    return router
