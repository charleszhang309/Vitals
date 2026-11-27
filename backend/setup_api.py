"""Host configuration over a direct loopback connection."""

import asyncio
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .config import CalendarSource, HealthConfig, OPENAI_HOSTS, Settings, load_settings, update_settings_file
from .monitor import Rules, send_webhook, utc_now


LOOPBACK = {"127.0.0.1", "::1"}
PRIVATE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
MACOS_TAILSCALE_IDS = ("io.tailscale.ipn.macos", "io.tailscale.ipn.macsys")
MACOS_TAILSCALE_EXECUTABLE = ("Contents", "MacOS", "Tailscale")
TAILSCALE_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)
CALENDAR_SECRETS = {
    "icloud": ("apple_id", "app_password"),
    "google": ("client_id", "client_secret", "refresh_token"),
    "outlook": ("client_id", "client_secret", "refresh_token"),
    "ics": ("url",),
}
CALENDAR_FIELDS = {"id", "name", "type", "color", "writable", "calendar_id", "tenant"}
AI_USAGE_KEYS = ("deepseek_api_key", "openai_admin_key", "anthropic_admin_key")
SECTION_FIELDS = {
    "weather": {"city_name", "latitude", "longitude"},
    "health": {"timezone"},
    "ai": {"model", "base_url", "api_key"},
    "ai_usage": set(AI_USAGE_KEYS),
    "webhook": {"kind", "device_key", "daily_briefing_time"},
    "calendar": {"sources"},
}


def direct_setup_request(scope, headers, parse_origin):
    peer = scope.get("client")
    if not peer or peer[0] not in LOOPBACK or len(headers.getlist("host")) != 1:
        return False
    if any(name == "forwarded" or name == "via" or name == "x-real-ip"
           or name.startswith(("x-forwarded-", "tailscale-")) for name in headers.keys()):
        return False
    authority = parse_origin(f'{scope.get("scheme", "http")}://{headers["host"]}')
    if not authority or authority[1] not in LOOPBACK:
        return False
    origins = headers.getlist("origin")
    return headers.get("sec-fetch-site") in {None, "none", "same-origin"} and (
        not origins or (len(origins) == 1 and parse_origin(origins[0]) == authority)
    )


class CityMatch(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    admin1: str | None = None
    country: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        return HealthConfig.check_timezone(value)


async def city_matches(client, query):
    try:
        response = await asyncio.wait_for(client.get(
            GEOCODING_URL, params={"name": query, "count": 6, "language": "zh", "format": "json"}, timeout=5,
        ), timeout=6)
        response.raise_for_status()
        document = response.json()
        rows = document.get("results", []) if isinstance(document, dict) and not document.get("error") else None
        if not isinstance(rows, list):
            raise ValueError("invalid location data")
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise HTTPException(502, "暂时无法查询城市，请稍后重试或手动填写") from None

    locations = []
    for row in rows[:6]:
        try:
            locations.append(CityMatch.model_validate(row))
        except ValidationError:
            continue
    return locations


def config_view(settings: Settings, running: Settings):
    return {
        "weather": settings.weather.model_dump(),
        "health": {"timezone": settings.health.timezone},
        "ai": {"model": settings.ai.model, "base_url": str(settings.ai.base_url),
               "has_api_key": bool(settings.ai.api_key)},
        "ai_usage": {f"has_{name}": bool(getattr(settings.ai_usage, name)) for name in AI_USAGE_KEYS},
        "webhook": {"kind": settings.webhook.kind, "daily_briefing_time": settings.webhook.daily_briefing_time,
                    "has_key": bool(settings.webhook.bark_url_template)},
        "calendar": {"sources": [
            {**source.model_dump(include=CALENDAR_FIELDS), "writable": source.can_write,
             **{f"has_{key}": bool(getattr(source, key)) for key in CALENDAR_SECRETS[source.type]}}
            for source in settings.calendar.sources
        ]},
        "agents": {"has_token": bool(settings.agents.token)},
        "restart_required": any(getattr(settings, section) != getattr(running, section)
                                for section in ("weather", "health", "webhook", "calendar", "agents", "ai_usage")) or any(
            getattr(settings.ai, key) != getattr(running.ai, key) for key in ("api_key", "model", "base_url")
        ),
    }


async def json_object(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 application/json")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 128 * 1024:
            raise HTTPException(413, "配置请求过大")
        data.extend(chunk)
    try:
        value = json.loads(data)
        if isinstance(value, dict):
            return value
    except (ValueError, RecursionError):
        pass
    raise HTTPException(422, "配置必须是有效的 JSON 对象")


def section_patch(document, patch):
    for section, values in patch.items():
        if section not in SECTION_FIELDS or not isinstance(values, dict) or values.keys() - SECTION_FIELDS[section]:
            raise HTTPException(422, "包含不支持的配置项")
        current = document.setdefault(section, {})
        if section == "calendar":
            if "sources" in values:
                current["sources"] = calendar_patch(current.get("sources", []), values["sources"])
            continue
        updates = values.copy()
        if section == "ai":
            key = updates.pop("api_key", None)
            if key is not None and not isinstance(key, str):
                raise HTTPException(422, "OpenAI API Key 无效")
            if key and key.strip():
                current["api_key"] = key.strip()
        if section == "ai_usage":
            for name, key in updates.items():
                if key is not None and (not isinstance(key, str) or len(key) > 4096):
                    raise HTTPException(422, "AI 用量密钥格式无效")
                if key and key.strip():
                    current[name] = key.strip()
            continue
        if section == "webhook":
            device_key = updates.pop("device_key", None)
            if device_key is not None and not isinstance(device_key, str):
                raise HTTPException(422, "Bark Device Key 无效")
            if device_key and device_key.strip():
                current["bark_url_template"] = f"https://api.day.app/{quote(device_key.strip(), safe='')}/{{title}}/{{body}}"
        current.update(updates)


def calendar_patch(existing, sources):
    if not isinstance(sources, list) or len(sources) > 16:
        raise HTTPException(422, "日历源最多 16 个")
    by_id = {source["id"]: source for source in existing}
    updated = []
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not isinstance(source.get("type"), str) or source["type"] not in CALENDAR_SECRETS):
            raise HTTPException(422, "日历源 ID 或类型无效")
        fields = CALENDAR_SECRETS[source["type"]]
        if source.keys() - (CALENDAR_FIELDS | set(fields)):
            raise HTTPException(422, "日历源包含不支持的字段")
        previous = by_id.get(source["id"], {})
        entry = previous.copy() if previous.get("type") == source["type"] else {}
        entry.update({key: value for key, value in source.items() if key not in fields})
        for key in fields:
            candidate = source.get(key)
            if candidate is not None and not isinstance(candidate, str):
                raise HTTPException(422, "日历凭据格式无效")
            if candidate and candidate.strip():
                entry[key] = candidate.strip()
        if any(not entry.get(key) for key in fields if key != "client_secret" or source["type"] == "google"):
            raise HTTPException(422, "请填写此日历源所需的凭据")
        if source["type"] == "ics":
            try:
                url = urlsplit(entry["url"])
                valid = url.scheme in {"https", "webcal"} and url.hostname and not url.username
            except ValueError:
                valid = False
            if not valid:
                raise HTTPException(422, "ICS 订阅地址须使用 https:// 或 webcal://")
            entry["writable"] = False
        CalendarSource.model_validate(entry)
        updated.append(entry)
    return updated


def write_config(path, update):
    try:
        return update_settings_file(path, update)
    except ValidationError as error:
        # Avoid returning the submitted values or user-defined dictionary keys.
        section = error.errors(include_input=False)[0]["loc"]
        label = {"weather": "天气", "health": "时区", "ai": "AI", "webhook": "Bark", "calendar": "日历"}.get(
            section[0] if section else None, "主机")
        raise HTTPException(422, f"{label}配置无效，请检查必填项和格式") from None
    except (ValueError, OSError):
        raise HTTPException(409, "无法更新配置文件，请检查文件权限或重新打开设置") from None


def running_macos_tailscale():
    try:
        processes = subprocess.run(["/bin/ps", "-axww", "-o", "comm="], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return
    for line in processes.stdout.splitlines():
        path = Path(line.strip())
        if not path.is_absolute() or path.parts[-3:] != MACOS_TAILSCALE_EXECUTABLE:
            continue
        bundle = path.parents[2]
        if bundle.suffix != ".app":
            continue
        try:
            with (bundle / "Contents" / "Info.plist").open("rb") as source:
                info = plistlib.load(source)
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get("CFBundleIdentifier") in MACOS_TAILSCALE_IDS:
            yield path


def tailscale_candidates():
    if sys.platform == "darwin":
        app_paths = (directory.joinpath("Tailscale.app", *MACOS_TAILSCALE_EXECUTABLE)
                     for directory in (Path("/Applications"), Path.home() / "Applications"))
        path_entries = (Path(directory) / "tailscale" for directory in os.get_exec_path() if Path(directory).is_absolute())
        candidates = (*running_macos_tailscale(), *app_paths, Path("/opt/homebrew/bin/tailscale"),
                      Path("/usr/local/bin/tailscale"), *path_entries)
        return tuple(str(path) for path in dict.fromkeys(candidates) if path.is_file() and os.access(path, os.X_OK))
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if root := os.environ.get(key):
                path = Path(root) / "Tailscale" / "tailscale.exe"
                if path.is_file():
                    return (str(path),)
    found = shutil.which("tailscale")
    return (found,) if found else ()


def run_tailscale(exe, *args):
    macos = sys.platform == "darwin"
    # Finder launches the sidecar without shell indicators. The bundled CLI
    # otherwise starts the GUI and may return an error as stdout with exit 0.
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"} if macos else None
    read_only = args in {("status", "--json"), ("serve", "status", "--json")}
    return subprocess.run([exe, *args], capture_output=True, stdin=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", env=env, timeout=5 if macos and read_only else 15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def tailscale_json(exe, *args):
    result = run_tailscale(exe, *args)
    if result.returncode != 0:
        raise OSError("Tailscale command failed")
    document = json.loads(result.stdout)
    if document is None:  # serve status returns null when unconfigured.
        return {}
    if not isinstance(document, dict):
        raise ValueError("Invalid Tailscale status")
    return document


def proxy_matches(handler, port):
    return handler.get("Proxy") in {f"http://{host}:{port}{slash}"
                                    for host in ("127.0.0.1", "localhost") for slash in ("", "/")}


def tailscale_status(candidates, port):
    result = {"installed": bool(candidates), "logged_in": False, "ip_addresses": [], "dns_name": None,
              "serve_enabled": False, "https_url": None, "blocked": False, "error": None}
    for exe in candidates:
        try:
            status = tailscale_json(exe, "status", "--json")
            state, node, ips = status.get("BackendState"), status.get("Self"), status.get("TailscaleIPs")
            if not isinstance(state, str) or not state or (node is not None and not isinstance(node, dict)):
                continue
            if ips is not None and (not isinstance(ips, list) or any(not isinstance(ip, str) for ip in ips)):
                continue
        except TAILSCALE_ERRORS:
            continue
        break
    else:
        if candidates:
            result["error"] = "无法读取 Tailscale 状态，请确认本机客户端正在运行"
        return None, result

    node = node or {}
    result["logged_in"] = state == "Running" and node.get("Online") is True
    result["ip_addresses"] = ips or []
    dns = node.get("DNSName")
    dns = dns.lower().rstrip(".") if isinstance(dns, str) else ""
    if re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", dns):
        result["dns_name"] = dns
    if not result["logged_in"] or not result["dns_name"]:
        return exe, result
    try:
        config = tailscale_json(exe, "serve", "status", "--json")
        authority = f"{dns}:443"
        handlers = ((config.get("Web") or {}).get(authority) or {}).get("Handlers") or {}
        root = handlers.get("/")
        listener = (config.get("TCP") or {}).get("443") or {}
        public = (config.get("AllowFunnel") or {}).get(authority)
        result["serve_enabled"] = bool(root is not None and proxy_matches(root, port) and listener.get("HTTPS") and not public)
        result["blocked"] = bool(public or (root is not None and not result["serve_enabled"])
                                 or (listener and not listener.get("HTTPS")))
        if result["serve_enabled"]:
            result["https_url"] = f"https://{dns}/"
    except (ValueError, OSError, subprocess.TimeoutExpired, AttributeError, TypeError):
        result["error"] = "已连接 Tailscale，但无法读取 Serve HTTPS 设置"
    return exe, result


def setup_router(path: Path, running: Settings):
    router = APIRouter(prefix="/api/setup", include_in_schema=False)
    serve_lock = asyncio.Lock()

    def read_config():
        try:
            return load_settings(path)
        except (ValueError, OSError):
            raise HTTPException(409, "无法读取主机配置文件") from None

    def view(settings):
        return JSONResponse(config_view(settings, running), headers=PRIVATE)

    @router.get("/status")
    def status():
        return JSONResponse({"service": "Vitals", "instance": os.environ.get("VITALS_DESKTOP_INSTANCE")}, headers=PRIVATE)

    @router.get("/config")
    def get_config():
        return view(read_config())

    @router.get("/geocode")
    async def geocode(request: Request, q: str = Query(max_length=100)) -> list[CityMatch]:
        query = q.strip()
        if not query:
            raise HTTPException(422, "请输入城市名")
        return await city_matches(request.app.state.external_client, query)

    @router.patch("/config")
    async def patch_config(request: Request):
        values = await json_object(request)

        def update(document, previous):
            section_patch(document, values)
            if "base_url" in values.get("ai", {}):
                url = Settings.model_validate(document).ai.base_url
                if url.scheme != "https" or url.host not in OPENAI_HOSTS or url.port not in (None, 443) or (url.path or "").rstrip("/") != "/v1":
                    raise HTTPException(422, "OpenAI 地址须为官方 HTTPS /v1 接口")

        return view(await run_in_threadpool(write_config, path, update))

    @router.post("/agents/token")
    async def agent_token(request: Request):
        payload = await json_object(request)
        if set(payload) != {"regenerate"} or type(payload["regenerate"]) is not bool:
            raise HTTPException(422, "regenerate 须为布尔值")
        if payload["regenerate"]:
            token = secrets.token_urlsafe(32)
            settings = await run_in_threadpool(write_config, path, lambda document, _:
                                                document.setdefault("agents", {}).update(token=token))
        else:
            settings = await run_in_threadpool(read_config)
        token = settings.agents.token
        return JSONResponse({"token": token.get_secret_value() if token else None,
                             "restart_required": settings.agents != running.agents}, headers=PRIVATE)

    @router.post("/webhook/test")
    async def test_bark(request: Request):
        await json_object(request)
        config = (await run_in_threadpool(read_config)).webhook
        if config.kind != "bark":
            raise HTTPException(409, "请先保存并启用 Bark")
        event = Rules.event("test", "Vitals 推送测试", "收到这条消息，说明 Vitals 通知已接通。", utc_now())
        try:
            await asyncio.wait_for(send_webhook(request.app.state.external_client, config, event), config.timeout_seconds)
        except Exception:
            raise HTTPException(502, "Bark 未接受推送，请检查 Device Key 或连接") from None
        return JSONResponse({"sent": True}, headers=PRIVATE)

    @router.get("/tailscale")
    def get_tailscale():
        _, current = tailscale_status(tailscale_candidates(), running.port)
        return JSONResponse(current, headers=PRIVATE)

    @router.post("/tailscale/serve")
    async def set_serve(request: Request):
        payload = await json_object(request)
        if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            raise HTTPException(422, "enabled 须为布尔值")
        enabled = payload["enabled"]
        async with serve_lock:
            exe, current = await run_in_threadpool(lambda: tailscale_status(tailscale_candidates(), running.port))
            if not (exe and current["logged_in"] and current["dns_name"]) or current["error"]:
                raise HTTPException(409, "请启动并登录 Tailscale，在管理后台开启 MagicDNS 和 HTTPS")
            if current["blocked"]:
                raise HTTPException(409, "HTTPS 443 根路径或 Funnel 已由其他规则占用，请先在 Tailscale 中检查")
            if current["serve_enabled"] != enabled:
                args = ["serve", "--bg", str(running.port)] if enabled else ["serve", "--bg", "--https=443", "off"]
                try:
                    result = await run_in_threadpool(run_tailscale, exe, *args)
                    _, current = await run_in_threadpool(tailscale_status, (exe,), running.port)
                    if result.returncode or current["serve_enabled"] != enabled or current["error"]:
                        raise OSError("Tailscale Serve did not change")
                except (OSError, subprocess.TimeoutExpired):
                    raise HTTPException(502, "Tailscale Serve 更新失败，请检查 HTTPS 设置和本机操作权限") from None
            return JSONResponse(current, headers=PRIVATE)

    return router
