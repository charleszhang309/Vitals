"""Download and validate GitHub ZIP packages without writing or importing plugin files."""

import asyncio
from dataclasses import dataclass
from io import BytesIO
import re
import stat
from struct import Struct
from unicodedata import normalize
from urllib.parse import unquote, urljoin, urlsplit
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
from zlib import error as ZlibError

import httpx

from .deck_plugins import PLUGIN_ID_PATTERN, parse_manifest


MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1024
MAX_DIRECTORY_BYTES = 512 * 1024
TOTAL_TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 3

_GITHUB_PROJECT = r"[A-Za-z0-9_.-]+"
_GITHUB_ARCHIVE = re.compile(
    rf"/{_GITHUB_PROJECT}/{_GITHUB_PROJECT}/(?:archive/.+\.zip|releases/(?:latest/download|download/[^/]+)/[^/]+\.zip)",
    re.IGNORECASE,
)
_CODELOAD_ARCHIVE = re.compile(rf"/{_GITHUB_PROJECT}/{_GITHUB_PROJECT}/(?:zip|legacy\.zip)/.+")
_GITHUB_ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_RELEASE_ASSET = re.compile(r"/github-production-release-asset(?:-[a-z0-9]+)?/[^/]+/[^/]+")
_REDIRECT_CODES = {301, 302, 303, 307, 308}

_END_OF_DIRECTORY = Struct("<4s4H2IH")
_EXTRA_HEADER = Struct("<HH")
_UNSUPPORTED_EXTRA_FIELDS = {0x000D, 0x756E}  # Legacy Unix fields can encode links and special types.
_SPECIAL_WINDOWS_ATTRIBUTES = 0x0008 | 0x0040 | 0x0400  # Volume label, device, reparse point.
_ZIP_PATH_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*]')
_WINDOWS_DEVICE = re.compile(
    r"(?:aux|clock\$|con|conin\$|conout\$|nul|prn|com[0-9¹²³]|lpt[0-9¹²³])", re.IGNORECASE,
)
_WINDOWS_SHORT_NAME = re.compile(r"~[0-9]+(?:\.|$)")
_MESSAGES = {
    "invalid_url": "Enter a GitHub HTTPS link to a source or release ZIP.",
    "download_failed": "The Deck plugin ZIP could not be downloaded from GitHub.",
    "too_large": "The Deck plugin archive exceeds size limits.",
    "invalid_archive": "The ZIP must contain one Deck plugin with safe, unambiguous file paths.",
    "invalid_manifest": "The Deck plugin manifest is invalid.",
}


class PluginArchiveError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class PluginPackage:
    id: str
    files: dict[str, bytes]


def _download_url(url: str, *, redirect: bool = False) -> str:
    if len(url) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in url) or any(
        char in url for char in "\\#"
    ):
        raise PluginArchiveError("invalid_url")
    try:
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        if host.endswith(":443"):
            host = host.removesuffix(":443")
        # Decode components individually to keep delimiters and dot segments unambiguous.
        components = [unquote(part, errors="strict") for part in parsed.path.split("/")[1:]]
    except ValueError:
        raise PluginArchiveError("invalid_url") from None
    if parsed.scheme != "https" or any(
        not part or part in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in part)
        or any(char in part for char in "/\\%?#") for part in components
    ):
        raise PluginArchiveError("invalid_url")

    path = "/" + "/".join(components)
    if host == "github.com" and not parsed.query and _GITHUB_ARCHIVE.fullmatch(path):
        return url
    if host == "codeload.github.com" and not parsed.query and _CODELOAD_ARCHIVE.fullmatch(path):
        return url
    if redirect and host in _GITHUB_ASSET_HOSTS and _RELEASE_ASSET.fullmatch(path):
        return url
    raise PluginArchiveError("invalid_url")


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False, follow_redirects=False) as client:
        for hop in range(MAX_REDIRECTS + 1):
            # Construct requests directly so a redirect never inherits the client's cookie jar.
            request = httpx.Request("GET", url, headers={"Accept-Encoding": "identity", "User-Agent": "Vitals-Deck"})
            response = await client.send(request, stream=True)
            try:
                if response.status_code in _REDIRECT_CODES:
                    if hop == MAX_REDIRECTS or not response.headers.get("Location"):
                        raise PluginArchiveError("download_failed")
                    try:
                        url = _download_url(urljoin(url, response.headers["Location"]), redirect=True)
                    except ValueError:
                        raise PluginArchiveError("download_failed") from None
                    continue
                if response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise PluginArchiveError("download_failed")
                length = response.headers.get("Content-Length")
                if length is not None and not length.isdecimal():
                    raise PluginArchiveError("download_failed")
                if length is not None and int(length) > MAX_ARCHIVE_BYTES:
                    raise PluginArchiveError("too_large")
                content = bytearray()
                async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > MAX_ARCHIVE_BYTES:
                        raise PluginArchiveError("too_large")
                    content.extend(chunk)
                if length is not None and int(length) != len(content):
                    raise PluginArchiveError("download_failed")
                return bytes(content)
            finally:
                await response.aclose()
    raise PluginArchiveError("download_failed")


async def download_github_archive(url: str) -> bytes:
    url = _download_url(url)
    try:
        return await asyncio.wait_for(_download(url), timeout=TOTAL_TIMEOUT_SECONDS)
    except PluginArchiveError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError, OSError, ValueError):
        raise PluginArchiveError("download_failed") from None


def _check_directory(content: bytes) -> int:
    # Bound the central directory before ZipFile allocates an object for each entry.
    offset = content.rfind(b"PK\x05\x06", max(0, len(content) - _END_OF_DIRECTORY.size - 65535))
    if offset < 0 or len(content) - offset < _END_OF_DIRECTORY.size:
        raise PluginArchiveError("invalid_archive")
    _, disk, directory_disk, disk_entries, count, size, directory_offset, comment_size = _END_OF_DIRECTORY.unpack_from(
        content, offset,
    )
    if disk or directory_disk or disk_entries != count or directory_offset + size != offset or (
        offset + _END_OF_DIRECTORY.size + comment_size != len(content)
    ):
        raise PluginArchiveError("invalid_archive")
    if count > MAX_ARCHIVE_ENTRIES or size > MAX_DIRECTORY_BYTES:
        raise PluginArchiveError("too_large")
    return count


def _safe_component(part: str) -> bool:
    return bool(part) and not (
        part[-1] in ". " or _ZIP_PATH_FORBIDDEN.search(part) or part.casefold() == ".git"
        or _WINDOWS_DEVICE.fullmatch(part.split(".")[0].rstrip(" ")) or _WINDOWS_SHORT_NAME.search(part)
    )


def _member_path(member: ZipInfo) -> tuple[str, ...]:
    # ZipInfo.filename can drop NULs and normalize backslashes on Windows.
    name = member.orig_filename.removesuffix("/")
    parts = tuple(name.split("/"))
    if len(name.encode("utf-16-le")) > 480 or len(parts) > 16 or not all(map(_safe_component, parts)):
        raise PluginArchiveError("invalid_archive")

    kind = stat.S_IFMT(member.external_attr >> 16)
    directory = member.is_dir()
    if (
        kind not in {0, stat.S_IFDIR if directory else stat.S_IFREG}
        or member.external_attr & _SPECIAL_WINDOWS_ATTRIBUTES
        or (member.external_attr & 0x10 and not directory)
        or (directory and member.file_size)
        or member.flag_bits & 1
        or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
    ):
        raise PluginArchiveError("invalid_archive")
    offset = 0
    while offset + _EXTRA_HEADER.size <= len(member.extra):
        kind, length = _EXTRA_HEADER.unpack_from(member.extra, offset)
        if kind in _UNSUPPORTED_EXTRA_FIELDS:
            raise PluginArchiveError("invalid_archive")
        offset += _EXTRA_HEADER.size + length
    if offset != len(member.extra):
        raise PluginArchiveError("invalid_archive")
    return parts


def _package_files(archive: ZipFile, count: int) -> dict[str, bytes]:
    members = archive.infolist()
    if len(members) != count:
        raise PluginArchiveError("invalid_archive")
    if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
        raise PluginArchiveError("too_large")

    entries = set()
    paths = {}
    files = {}
    for member in members:
        if member.file_size > MAX_ARCHIVE_BYTES:
            raise PluginArchiveError("too_large")
        parts = _member_path(member)
        normalized = tuple(normalize("NFC", part).casefold() for part in parts)
        if normalized in entries:
            raise PluginArchiveError("invalid_archive")
        entries.add(normalized)
        for length in range(1, len(parts) + 1):
            key = normalized[:length]
            kinds = (parts[:length], length < len(parts) or member.is_dir())
            if paths.setdefault(key, kinds) != kinds:
                raise PluginArchiveError("invalid_archive")
        # This checks the local header against the directory for every member without inflating it.
        with archive.open(member):
            pass
        if not member.is_dir():
            files[parts] = member

    roots = [parts[:-1] for parts in files if parts[-1] == "manifest.json" and (*parts[:-1], "plugin.py") in files]
    if len(roots) != 1 or len(roots[0]) > 2:
        raise PluginArchiveError("invalid_archive")
    root = roots[0]
    if root and any(key[0] != root[0] for key, _ in paths.values()):
        raise PluginArchiveError("invalid_archive")
    return {
        "/".join(parts[len(root):]): archive.read(member)
        for parts, member in sorted(files.items()) if parts[:len(root)] == root
    }


def extract_package(archive_bytes: bytes) -> PluginPackage:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise PluginArchiveError("too_large")
    count = _check_directory(archive_bytes)
    try:
        with ZipFile(BytesIO(archive_bytes)) as archive:
            files = _package_files(archive, count)
    except PluginArchiveError:
        raise
    except (BadZipFile, EOFError, NotImplementedError, OSError, RuntimeError, ValueError, ZlibError):
        raise PluginArchiveError("invalid_archive") from None

    try:
        manifest = parse_manifest(files["manifest.json"])
    except (RecursionError, ValueError):
        raise PluginArchiveError("invalid_manifest") from None
    if not re.fullmatch(PLUGIN_ID_PATTERN, manifest.id) or not _safe_component(manifest.id):
        raise PluginArchiveError("invalid_manifest")
    return PluginPackage(manifest.id, files)
