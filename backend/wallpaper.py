"""提供一个在设备之间共享的静态图片壁纸。"""

from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from .config import MIB


MAX_INPUT = 10 * MIB
MAX_PIXELS = 40_000_000
FORMATS = ["PNG", "JPEG", "WEBP"]


def encode_wallpaper(data: bytearray) -> bytes:
    try:
        with Image.open(BytesIO(data), formats=FORMATS) as original:
            if getattr(original, "n_frames", 1) != 1 or original.width * original.height > MAX_PIXELS:
                raise HTTPException(422, "请选择 4000 万像素以内的静态图片")
            image = ImageOps.exif_transpose(original)
            image.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
            # 统一输出静态 WebP，剥离原图元数据与任意附加内容。
            with BytesIO() as output:
                image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB").save(
                    output, format="WEBP", quality=86,
                )
                return output.getvalue()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise HTTPException(415, "请选择有效的 PNG、JPEG 或 WebP 图片") from error


def wallpaper_router(data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/wallpaper", tags=["wallpaper"])
    path = data_dir / "wallpaper.webp"
    lock = Lock()
    encode_lock = Lock()

    def version() -> str | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    @router.get("")
    def get_wallpaper(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with lock:
            return {"version": version()}

    @router.get("/image", response_class=Response)
    def get_image():
        with lock:
            try:
                image = path.read_bytes()
            except FileNotFoundError as error:
                raise HTTPException(404, "尚未设置壁纸", headers={"Cache-Control": "no-store"}) from error
        return Response(image, media_type="image/webp", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    def save_image(data: bytearray) -> str | None:
        with encode_lock:
            image = encode_wallpaper(data)
        temporary = None
        try:
            with NamedTemporaryFile(dir=data_dir, prefix=".wallpaper-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(image)
            with lock:
                temporary.replace(path)
                return version()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @router.put("", openapi_extra={"requestBody": {"required": True, "content": {
        media_type: {"schema": {"type": "string", "format": "binary"}}
        for media_type in ("image/png", "image/jpeg", "image/webp")
    }}})
    async def set_wallpaper(request: Request):
        content_length = request.headers.get("content-length", "")
        if content_length.isdecimal() and int(content_length) > MAX_INPUT:
            raise HTTPException(413, "壁纸图片上限为 10 MiB")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_INPUT:
                raise HTTPException(413, "壁纸图片上限为 10 MiB")
            data.extend(chunk)
        return {"version": await run_in_threadpool(save_image, data)}

    @router.delete("", status_code=204)
    def reset_wallpaper():
        with lock:
            path.unlink(missing_ok=True)
        return Response(status_code=204)

    return router
