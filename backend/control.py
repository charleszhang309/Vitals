"""通过配置白名单切换 Windows 电源计划和可选的风扇配置。"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
from threading import Lock, Thread
from time import monotonic, sleep, time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import ROOT, Settings
from .monitor import Rules, send_webhook, utc_now


logger = logging.getLogger(__name__)
GUID = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)
FAN_MODE_FLOORS = {"silent": 30, "balanced": 45, "performance": 0}
DESKTOP_ACTIONS = {"lock", "display-off", "probe"}


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(min_length=1, max_length=32)


class DesktopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["lock", "display-off"]
    confirmed: StrictBool = False


class ShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(strict=True, ge=1, le=1440)
    confirmed: StrictBool = False


def dispatch_desktop(action: str):
    if action not in DESKTOP_ACTIONS:
        raise ValueError("unrecognized desktop action")
    if os.name != "nt":
        raise HTTPException(503, "桌面控制需要 Windows")
    token = f"{int(time()) + 10}-{uuid4().hex}"
    results = ROOT / "data" / ".desktop-actions"
    receipt = results / (token + ".txt")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # 计划任务使用同一账号的交互令牌。传入值只有内部动作名与限时标识。
    script = ("$ErrorActionPreference='Stop'; $scheduler=New-Object -ComObject 'Schedule.Service'; "
              "$scheduler.Connect(); $task=$scheduler.GetFolder('\\').GetTask('Vitals Desktop Control'); "
              f"$task.Run([string[]]@('{action}','{token}')) | Out-Null")
    try:
        results.mkdir(parents=True, exist_ok=True)
        deadline = monotonic() + 10
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                       capture_output=True, timeout=6, creationflags=subprocess.CREATE_NO_WINDOW)
        while monotonic() < deadline:
            if receipt.is_file():
                if receipt.read_text(encoding="ascii") == "accepted":
                    return
                raise HTTPException(503, "Windows 未接受桌面操作，请检查当前登录会话")
            sleep(0.10)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        logger.info("桌面控制派发失败（%s）", type(error).__name__)
        raise HTTPException(503, "无法启动桌面控制，请确认 Windows 已登录且 Vitals Desktop Control 任务已安装") from None
    finally:
        try:
            receipt.unlink(missing_ok=True)
        except OSError:
            logger.info("未能清理桌面操作结果")
    raise HTTPException(503, "桌面会话未响应，请确认 Windows 已登录且桌面控制任务可用")


def parse_active_scheme(output: str) -> tuple[UUID, str | None]:
    for line in output.splitlines():
        match = GUID.search(line)
        if match:
            # Windows 的前缀会本地化，使用 GUID 定位计划名。
            name = re.fullmatch(r"\s*(?:\((.*)\)|（(.*)）)\s*", line[match.end():])
            return UUID(match[0]), (name[1] if name[1] is not None else name[2]) if name else None
    raise ValueError("powercfg 输出中没有 GUID")


def powercfg(*args: str) -> str:
    if os.name != "nt":
        raise HTTPException(503, "电源计划控制需要 Windows")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "powercfg.exe"
    try:
        result = subprocess.run(
            [str(exe), *args], check=True, capture_output=True, timeout=8,
            encoding="oem", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        logger.warning("powercfg %s 超时", args[0])
        raise HTTPException(504, "Windows 电源计划命令超时") from error
    except (OSError, subprocess.CalledProcessError) as error:
        logger.warning("powercfg %s 失败: %s", args[0], getattr(error, "stderr", None) or error)
        raise HTTPException(503, "Windows 电源计划命令失败，请检查 GUID 和运行权限") from error
    return result.stdout


def dispatch_fan_profile(exe: Path | None, profile: Path | None) -> dict[str, str]:
    if exe is None or profile is None:
        return {"status": "skipped"}
    try:
        exe = (ROOT / exe).resolve(strict=True)
        profile = (ROOT / profile).resolve(strict=True)
        if not exe.is_file() or not profile.is_file():
            raise OSError("FanControl 可执行文件或配置文件无效")
        # FanControl 可能会启动一个长期运行的实例，API 只等待命令派发。
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(exe), "-c", str(profile)], cwd=exe.parent, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )
        status = process.poll()
        if status is None:
            Thread(target=process.wait, name="fancontrol-wait", daemon=True).start()
        elif status != 0:
            raise OSError(f"FanControl 退出代码 {status}")
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("FanControl 派发失败: %s", error)
        return {"status": "failed", "message": "电源计划已切换，FanControl 配置派发失败"}
    return {"status": "dispatched"}


def shutdown_windows():
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe"
    subprocess.run([str(exe), "/s", "/t", "0"], check=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)


def update_fan_mode_sensor(path: Path, mode: str | None) -> dict[str, str]:
    # Fan Control 的文件传感器只接收数值；真实温控曲线独立决定散热下限。
    value = f"{FAN_MODE_FLOORS.get(mode, 45)}\n"
    try:
        try:
            if path.read_text(encoding="ascii") == value:
                return {"status": "sensor_updated"}
        except (FileNotFoundError, UnicodeError):
            pass
        temporary = None
        try:
            with NamedTemporaryFile(dir=path.parent, prefix=".fan-mode-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(value.encode("ascii"))
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("无法更新 FanControl 档位文件（%s）", type(error).__name__)
        return {"status": "failed", "message": "Windows 电源计划已生效，风扇档位文件更新失败"}
    return {"status": "sensor_updated"}


class PowerControl:
    def __init__(self, settings: Settings, sensor_file: Path | None = None):
        self.settings = settings
        self.sensor_file = sensor_file
        self.lock = Lock()
        self.desktop_lock = Lock()
        self.latest: dict | None = None
        self.shutdown_at: datetime | None = None
        self.shutdown_task: asyncio.Task | None = None

    def active_power(self):
        with self.lock:
            output = powercfg("/getactivescheme")
            try:
                guid, name = parse_active_scheme(output)
            except ValueError as error:
                raise HTTPException(502, "无法解析 Windows 电源计划") from error
            modes = self.settings.power_modes
            matching = next((key for key, preset in modes.items() if preset.guid == guid), None)
            self.latest = {"guid": str(guid), "name": name, "mode": matching, "modes": list(modes)}
            if self.sensor_file is not None:
                update_fan_mode_sensor(self.sensor_file, matching)
            return self.latest

    def set_power(self, mode: str):
        preset = self.settings.power_modes.get(mode)
        if preset is None:
            raise HTTPException(400, "未配置该电源模式")
        with self.lock:
            powercfg("/setactive", str(preset.guid))
            self.latest = {"guid": str(preset.guid), "name": None, "mode": mode, "modes": list(self.settings.power_modes)}
            if self.sensor_file is not None:
                fan = update_fan_mode_sensor(self.sensor_file, mode)
            else:
                fan = dispatch_fan_profile(self.settings.fancontrol_exe, preset.fan_profile)
        return {"guid": str(preset.guid), "mode": mode, "fan_control": fan}

    def confirmed_host_action(self, action: str):
        if action not in {"lock", "display_off", "display_lock", "sleep"}:
            raise HTTPException(422, "不支持此 Windows 桌面操作")
        if not self.desktop_lock.acquire(blocking=False):
            raise HTTPException(409, "有桌面操作正在执行，请稍后重试")
        try:
            if action == "sleep":
                if os.name != "nt":
                    raise HTTPException(503, "休眠控制需要 Windows")
                powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                script = ("Add-Type -AssemblyName System.Windows.Forms; "
                          "if (-not [Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)) { exit 1 }")
                try:
                    subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                                   timeout=15, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.SubprocessError):
                    raise HTTPException(503, "Windows 未接受睡眠请求") from None
            else:
                if action in {"lock", "display_lock"}:
                    dispatch_desktop("lock")
                if action in {"display_off", "display_lock"}:
                    dispatch_desktop("display-off")
        finally:
            self.desktop_lock.release()

    def schedule_shutdown(self, minutes: int) -> str:
        if os.name != "nt":
            raise HTTPException(503, "定时关机需要 Windows")
        if not 1 <= minutes <= 1440:
            raise HTTPException(400, "关机时间必须介于 1 到 1440 分钟")
        if self.shutdown_task is not None and not self.shutdown_task.done():
            raise HTTPException(409, "已有定时关机，请先取消")
        self.shutdown_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.shutdown_task = asyncio.create_task(self._shutdown_after(minutes * 60), name="scheduled-shutdown")
        return self.shutdown_at.isoformat(timespec="seconds")

    def cancel_shutdown(self):
        if self.shutdown_at is None or self.shutdown_task is None:
            raise HTTPException(404, "没有可以取消的定时关机")
        self.shutdown_at = None
        self.shutdown_task.cancel()
        self.shutdown_task = None

    async def _shutdown_after(self, delay: int):
        await asyncio.sleep(delay)
        self.shutdown_at = None
        try:
            # 等待由 Vitals 负责，避免 Windows 的非零 /t 隐式启用强制关机。
            await asyncio.to_thread(shutdown_windows)
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("定时关机失败（%s）", type(error).__name__)

    async def close(self):
        task = self.shutdown_task
        if task is not None:
            if self.shutdown_at is not None:
                self.cancel_shutdown()
            await asyncio.gather(task, return_exceptions=True)


def control_router(control: PowerControl) -> APIRouter:
    router = APIRouter(prefix="/api/control", tags=["control"])
    router.add_api_route("/power", control.active_power, methods=["GET"])

    @router.post("/power")
    def set_power(request: ModeRequest):
        return control.set_power(request.mode)

    @router.get("/quick")
    async def quick_status():
        when = control.shutdown_at
        return {"shutdown_at": when.isoformat(timespec="seconds") if when else None}

    @router.post("/desktop")
    def desktop(payload: DesktopRequest):
        if payload.action == "lock" and not payload.confirmed:
            raise HTTPException(409, "请先确认锁屏")
        control.confirmed_host_action(payload.action.replace("-", "_"))
        return {"accepted": True, "action": payload.action}

    @router.post("/shutdown")
    async def shutdown(payload: ShutdownRequest):
        if not payload.confirmed:
            raise HTTPException(409, "请先确认定时关机")
        return {"shutdown_at": control.schedule_shutdown(payload.minutes)}

    @router.delete("/shutdown")
    async def cancel_shutdown():
        control.cancel_shutdown()
        return {"shutdown_at": None}

    @router.post("/webhook/test")
    async def test_webhook(request: Request):
        config = control.settings.webhook
        if config.kind == "disabled":
            raise HTTPException(409, "请在服务器配置并启用 Webhook")
        event = Rules.event("test", "Vitals 推送测试", "收到这条消息，说明 Vitals 通知已接通。", utc_now())
        try:
            await asyncio.wait_for(send_webhook(request.app.state.external_client, config, event), config.timeout_seconds)
        except Exception as error:
            logger.info("测试推送失败（%s）", type(error).__name__)
            raise HTTPException(502, "推送未被接收，请检查服务器的 Webhook 配置") from None
        return {"sent": True, "provider": config.kind}

    return router
