"""Dispatch configured application entries with no arguments from callers."""

from pathlib import Path
import subprocess
from threading import Thread
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as PathParameter, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from .config import LAUNCHER_ID_PATTERN, Launcher, load_settings


LauncherId = Annotated[str, PathParameter(pattern=LAUNCHER_ID_PATTERN)]


def launch_app(entry: Launcher, platform: str) -> None:
    configured = Path(entry.target)
    try:
        if not configured.is_absolute():
            raise ValueError("configured path must be absolute on this host")
        target = configured.resolve(strict=True)
        if (platform == "windows" and target.suffix.lower() == ".exe"
                and not target.as_posix().startswith("//") and target.is_file()):
            process = subprocess.Popen(
                [str(target)], cwd=target.parent, shell=False, close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # 窗口应用可能长期运行，后台回收子进程，不让 HTTP 等待。
            status = process.poll()
            if status is None:
                Thread(target=process.wait, name="launcher-wait", daemon=True).start()
            elif status != 0:
                raise RuntimeError("application exited unsuccessfully")
        elif platform == "macos" and target.suffix == ".app" and target.is_dir():
            subprocess.run(
                ["/usr/bin/open", "-a", str(target)], shell=False, check=True, timeout=5,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            raise ValueError("configured application is unavailable on this platform")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise HTTPException(503, "无法启动应用，请在主机配置检查路径和运行权限") from None


def launchers_router(config_path: Path | None, entries: list[Launcher], platform: str):
    router = APIRouter(prefix="/api/control/launchers")

    def configured_entries() -> list[Launcher]:
        if config_path is None:
            return entries
        try:
            return load_settings(config_path).launchers
        except (OSError, ValueError):
            raise HTTPException(503, "无法读取主机启动项配置") from None

    def find(launcher_id: str) -> Launcher:
        entry = next((entry for entry in configured_entries() if entry.id == launcher_id), None)
        if entry is None:
            raise HTTPException(404, "找不到该启动项")
        return entry

    @router.get("")
    def list_launchers(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"items": [
            {**entry.model_dump(exclude={"target"}), **({"url": entry.target} if entry.kind == "url" else {})}
            for entry in configured_entries()
        ]}

    @router.post("/{launcher_id}", status_code=202)
    async def dispatch(launcher_id: LauncherId, request: Request):
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(422, "只能发送启动项 ID")
        entry = await run_in_threadpool(find, launcher_id)
        if entry.kind != "app":
            raise HTTPException(409, "链接启动项应在浏览器中打开")
        await run_in_threadpool(launch_app, entry, platform)
        return {"status": "dispatched"}

    @router.get("/{launcher_id}/open", include_in_schema=False)
    def open_link(launcher_id: LauncherId):
        # 桌面 WebView 通过同源地址把链接交给系统浏览器。
        entry = find(launcher_id)
        if entry.kind != "url":
            raise HTTPException(404, "该启动项没有网页地址")
        return RedirectResponse(entry.target, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    return router
