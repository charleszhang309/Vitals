"""Current app version and cached GitHub release checks."""

import asyncio
from hashlib import sha256
import platform
import re
import sys
from time import monotonic

from fastapi import APIRouter, HTTPException, Request, Response
import httpx


APP_VERSION = "3.0.1"
REPOSITORY = "https://github.com/charleszhang309/Vitals"
RELEASES_URL = f"{REPOSITORY}/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/charleszhang309/Vitals/releases/latest"
RELEASE_TTL = 15 * 60
SEMVER = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
ARCH = re.compile(r"(?<![a-z0-9])(arm64|aarch64|amd64|x86_64|x64|i[3-6]86|x86|universal)(?![a-z0-9])")


def semver_key(version):
    match = SEMVER.fullmatch(version) if isinstance(version, str) and len(version) <= 120 else None
    if match is None:
        raise ValueError("invalid semantic version")
    major, minor, patch, prerelease = match.groups()
    identifiers = prerelease.split(".") if prerelease else []
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in identifiers):
        raise ValueError("invalid prerelease identifier")
    priority = tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers)
    return int(major), int(minor), int(patch), not prerelease, priority


def architecture(name):
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(name, name)


def choose_download(assets, host_platform, fallback):
    extension = {"windows": ".exe", "macos": ".dmg"}.get(host_platform)
    if not extension or not isinstance(assets, list):
        return fallback
    target = architecture(platform.machine().lower())
    candidates = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        if not (isinstance(name, str) and name.lower().endswith(extension)
                and isinstance(url, str) and url.lower().startswith(f"{RELEASES_URL.lower()}/download/")):
            continue
        arch = ARCH.search(name.lower())
        machine = architecture(arch[1]) if arch else None
        if machine not in {target, "universal", None}:
            continue
        installer = "setup" in name.lower() or "installer" in name.lower()
        candidates.append(((machine == target, installer, machine == "universal"), url))
    return max(candidates, default=(None, fallback), key=lambda item: item[0])[1]


def release_details(document, host_platform):
    result = {"current_version": APP_VERSION, "latest_version": None, "has_update": False,
              "release_url": RELEASES_URL, "notes": "", "download_url": RELEASES_URL}
    if document is None:
        return result
    tag = document.get("tag_name")
    latest = semver_key(tag)
    page = document.get("html_url")
    if isinstance(page, str) and page.lower().startswith(f"{RELEASES_URL.lower()}/tag/"):
        result["release_url"] = page
    notes = document.get("body")
    result.update(latest_version=tag.removeprefix("v"), has_update=latest > semver_key(APP_VERSION),
                  notes=notes[:16_000] if isinstance(notes, str) else "",
                  download_url=choose_download(document.get("assets"), host_platform, result["release_url"]))
    return result


def updates_router(host_platform, index_path):
    router = APIRouter(prefix="/api/system")
    lock = asyncio.Lock()
    result = None
    expires = 0.0

    @router.get("/version")
    def version(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            build_id = sha256(index_path.read_bytes()).hexdigest()[:16]
        except OSError:
            build_id = None
        return {"version": APP_VERSION, "build_id": build_id,
                "platform": host_platform, "desktop": bool(getattr(sys, "frozen", False))}

    @router.get("/update/check")
    async def check_updates(request: Request):
        nonlocal result, expires
        async with lock:
            if monotonic() >= expires:
                try:
                    response = await asyncio.wait_for(request.app.state.external_client.get(
                        LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": f"Vitals/{APP_VERSION}"},
                        timeout=5,
                    ), timeout=6)
                    if response.status_code == 404:
                        result = release_details(None, host_platform)
                    else:
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("invalid release data")
                        result = release_details(payload, host_platform)
                    expires = monotonic() + RELEASE_TTL
                except (httpx.HTTPError, TimeoutError, ValueError):
                    result = None
                    expires = monotonic() + 30
            if result is None:
                raise HTTPException(502, "暂时无法从 GitHub 查找更新，请稍后重试")
            return result

    return router
