"""Validate and serve shared Deck icons from the hub's data directory."""

import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from xml.etree import ElementTree

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

ICON_URL = "/api/deck/icons"
MAX_ICON_BYTES = 512 * 1024
MAX_MULTIPART_BYTES = MAX_ICON_BYTES + 16 * 1024
MAX_RASTER_PIXELS = 16_777_216
MAX_ICON_VALUE_LENGTH = len(ICON_URL) + 1 + 64 + len(".webp")
ICON_FILENAME = re.compile(r"[0-9a-f]{64}\.(png|jpg|webp|svg)")
LUCIDE_ICON = re.compile(r"lucide:[a-z0-9]+(?:-[a-z0-9]+)*")
URI_PREFIX = re.compile(r"[/\\]|[a-zA-Z][a-zA-Z0-9+.-]*:")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
RASTER_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SVG_ELEMENTS = frozenset("""
    svg g defs title desc path rect circle ellipse line polyline polygon
    linearGradient radialGradient stop clipPath mask text tspan
""".split())
SVG_ATTRIBUTES = frozenset("""
    id version viewBox preserveAspectRatio width height x y x1 y1 x2 y2
    cx cy r rx ry fx fy fr d points pathLength transform vector-effect
    fill-opacity fill-rule stroke-opacity stroke-width stroke-linecap stroke-linejoin
    stroke-miterlimit stroke-dasharray stroke-dashoffset opacity
    gradientUnits gradientTransform spreadMethod offset stop-opacity
    clipPathUnits clip-rule maskUnits maskContentUnits mask-type
    dominant-baseline text-anchor dx dy font-size font-weight
""".split())
PAINT_ATTRIBUTES = {"fill", "stroke", "color", "stop-color"}
FRAGMENT = r"url\(#[A-Za-z_][A-Za-z0-9_.:-]*\)"
PAINT = re.compile(rf"[a-z]+|#[0-9a-f]{{3,8}}|(?:rgb|hsl)a?\([0-9.,% +/\-]+\)|{FRAGMENT}", re.IGNORECASE | re.ASCII)
LOCAL_RESOURCE = re.compile(rf"none|{FRAGMENT}")
IMAGE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def validate_icon_value(icon: str) -> str:
    if LUCIDE_ICON.fullmatch(icon):
        return icon
    if icon.startswith(f"{ICON_URL}/") and ICON_FILENAME.fullmatch(icon[len(ICON_URL) + 1:]):
        return icon
    if len(icon) <= 16 and not URI_PREFIX.match(icon.lstrip()):
        return icon
    raise ValueError("Deck 图标必须使用 Lucide 名称、已上传的图片或简短的 Emoji")


class IconTreeBuilder(ElementTree.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = 0

    def start(self, tag, attrs):
        self.depth += 1
        if self.depth > 64:
            raise ValueError("SVG nesting is too deep")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)

    def doctype(self, name, pubid, system):
        raise ValueError("SVG document types are unsupported")

    def pi(self, target, data):
        raise ValueError("SVG processing instructions are unsupported")


def normalize_svg(data: bytes) -> bytes:
    root = ElementTree.fromstring(data, parser=ElementTree.XMLParser(target=IconTreeBuilder()))
    namespace = f"{{{SVG_NAMESPACE}}}" if root.tag.startswith(f"{{{SVG_NAMESPACE}}}") else ""
    if root.tag != f"{namespace}svg":
        raise ValueError("Expected an SVG document")
    for element in root.iter():
        tag = element.tag.removeprefix(namespace)
        if tag not in SVG_ELEMENTS or (namespace and element.tag == tag):
            raise ValueError("Unsupported SVG element")
        element.tag = tag
        for name, value in element.attrib.items():
            if name in PAINT_ATTRIBUTES and PAINT.fullmatch(value.strip()):
                continue
            if name in {"clip-path", "mask"} and LOCAL_RESOURCE.fullmatch(value.strip()):
                continue
            if name not in SVG_ATTRIBUTES:
                raise ValueError("Unsupported SVG attribute")
    root.set("xmlns", SVG_NAMESPACE)
    return ElementTree.tostring(root, encoding="utf-8")


def read_image(data: bytes, filename: str) -> tuple[bytes, str]:
    extension = Path(filename).suffix.lower().removeprefix(".")
    if extension == "jpeg":
        extension = "jpg"
    if extension not in MEDIA_TYPES:
        raise HTTPException(415, "请选择 PNG、JPEG、WebP 或 SVG 图片")
    try:
        if extension == "svg":
            data = normalize_svg(data)
        else:
            with Image.open(BytesIO(data), formats=[RASTER_FORMATS[extension]]) as image:
                if image.width * image.height > MAX_RASTER_PIXELS:
                    raise HTTPException(422, "Deck 图标不能超过 16777216 像素")
                image.load()
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as error:
        raise HTTPException(415, "Deck 图标格式无效或 SVG 包含不受支持的内容") from error
    return data, extension


def bounded_multipart(request: Request) -> Request:
    size_error = HTTPException(413, "Deck 图标大小上限为 512 KiB")
    length = request.headers.get("content-length", "")
    if length.isascii() and length.isdecimal() and (len(length) > 10 or int(length) > MAX_MULTIPART_BYTES):
        raise size_error
    consumed = 0

    async def receive():
        nonlocal consumed
        message = await request.receive()
        if message["type"] == "http.request":
            consumed += len(message.get("body", b""))
            if consumed > MAX_MULTIPART_BYTES:
                raise size_error
        return message

    return Request(request.scope, receive=receive)


def deck_icons_router(data_dir: Path) -> APIRouter:
    router = APIRouter(prefix=ICON_URL, tags=["deck"])
    directory = data_dir / "deck_icons"
    save_lock = Lock()

    def store(data: bytes, filename: str) -> dict[str, str]:
        image, extension = read_image(data, filename)
        icon_id = f"{sha256(image).hexdigest()}.{extension}"
        path = directory / icon_id
        with save_lock:
            if not path.is_file():
                directory.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with NamedTemporaryFile(dir=directory, prefix=".icon-", suffix=".tmp", delete=False) as file:
                        temporary = Path(file.name)
                        file.write(image)
                    temporary.replace(path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        return {"id": icon_id, "url": f"{ICON_URL}/{icon_id}"}

    @router.post("", status_code=201, openapi_extra={"requestBody": {"required": True, "content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary"},
        }}},
    }}})
    async def upload(request: Request):
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "multipart/form-data":
            raise HTTPException(422, "请选择要上传的 Deck 图标文件")
        try:
            async with bounded_multipart(request).form(max_files=1, max_fields=0) as form:
                file = form.get("file")
                if not isinstance(file, UploadFile) or not file.filename:
                    raise HTTPException(422, "请选择要上传的 Deck 图标文件")
                data = await file.read(MAX_ICON_BYTES + 1)
                if len(data) > MAX_ICON_BYTES:
                    raise HTTPException(413, "Deck 图标大小上限为 512 KiB")
                return await run_in_threadpool(store, data, file.filename)
        except MultipartParseError as error:
            raise HTTPException(400, "Deck 图标上传格式无效") from error

    @router.get("/{icon_id}", response_class=FileResponse)
    def get_icon(icon_id: str):
        match = ICON_FILENAME.fullmatch(icon_id)
        if not match or not (directory / icon_id).is_file():
            raise HTTPException(404, "找不到该 Deck 图标")
        return FileResponse(directory / icon_id, media_type=MEDIA_TYPES[match[1]], headers=IMAGE_HEADERS)

    return router
