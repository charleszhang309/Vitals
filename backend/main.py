"""FastAPI 入口与 Uvicorn 启动配置。"""

import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext, suppress
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import sys
from threading import Lock, Thread
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import StrictBool, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from websockets.exceptions import ConnectionClosed

from .agents import PROFILE_INTERVAL, AgentRegistry, agents_router, require_agent_token
from .ai import AiState, DailyBriefing, ai_router
from .ai_tools import AiTools
from .ai_usage import AiUsage, UsageLedger, usage_router
from .backup import Backups, backup_router
from .calendar_service import CalendarService, calendar_router
from .config import ConfigModel, MIB, ROOT, SamplingMode, Settings, load_settings, update_settings_file
from .deck import DeckStore, deck_router
from .deck_actions import DeckActions
from .deck_plugin_manager import DeckPluginManager, deck_plugin_router
from .deck_icons import deck_icons_router
from .deck_plugins import DeckPluginRegistry
from .control import MacPowerControl, PowerControl, control_router
from .expenses import ExpenseStore, expenses_router
from .health import HealthStore, health_router
from .life import LifeStore, life_router
from .launchers import launchers_router
from .lyrics import LyricsCache
from .media import MacMediaBridge, MediaBridge, media_router
from .monitor import Monitor, host_identity
from .rates import RatePins, Rates, rates_router
from .rate_history import RateHistory
from .scratchpad import Scratchpad, scratchpad_router
from .setup_api import direct_setup_request, setup_router
from .screenshot import screenshot_router
from .updates import updates_router
from .wallpaper import wallpaper_router
from .weather import Weather, weather_router


TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HTTP_AUTHORITY = re.compile(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?")
DISCONNECT_ERRORS = (WebSocketDisconnect, RuntimeError, OSError, ConnectionClosed)
logger = logging.getLogger(__name__)


class SamplingPreference(ConfigModel):
    mode: SamplingMode


class MetricsClientState(ConfigModel):
    type: Literal["client_state"]
    visible: StrictBool
    active_telemetry: StrictBool


def local_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address in TAILNET_V4 or address in TAILNET_V6


def http_origin(value: str) -> tuple[str, str, int] | None:
    if any(char.isspace() or ord(char) < 32 or char in "?#" for char in value):
        return None
    try:
        authority = urlsplit(value)
        hostname = authority.hostname
        port = authority.port
    except ValueError:
        return None
    if (authority.scheme not in {"http", "https"} or not hostname or authority.username is not None
            or authority.path or not HTTP_AUTHORITY.fullmatch(authority.netloc) or port == 0):
        return None
    default_port = 443 if authority.scheme == "https" else 80
    return authority.scheme, hostname, port or default_port


def trusted_host(value: str) -> bool:
    authority = http_origin(f"http://{value}")
    if authority is None:
        return False
    _, hostname, _ = authority
    if local_address(hostname):
        return True
    # 允许 MagicDNS 短名和完整设备名，限制浏览器可使用的其他域名。
    labels = hostname.rstrip(".").split(".")
    return len(labels) in (1, 4) and (len(labels) == 1 or labels[-2:] == ["ts", "net"]) and all(
        DNS_LABEL.fullmatch(label) for label in labels
    )


class PrivateNetwork:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(f"{root_path}/"):
            path = path.removeprefix(root_path)
        if path == "/api/setup" or path.startswith("/api/setup/"):
            if scope_type == "http" and direct_setup_request(scope, headers, http_origin):
                await self.app(scope, receive, send)
            elif scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "主机配置仅允许本机直连访问"}, 403,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        allowed = bool(peer and local_address(peer[0]) and trusted_host(host))
        if allowed and (scope_type == "websocket" or scope["method"] not in {"GET", "HEAD", "OPTIONS"} or path.startswith("/api/deck")
                        or scope.get("path", "").rstrip("/") in {"/api/ai/briefing", "/api/media/lyrics", "/api/system/backup"}):
            origin = headers.get("origin")
            parsed_origin = http_origin(origin) if origin is not None else None
            allowed = headers.get("sec-fetch-site") != "cross-site" and (
                origin is None or (parsed_origin is not None and parsed_origin in {
                    http_origin(f"http://{host}"), http_origin(f"https://{host}")
                })
            )
        if allowed:
            await self.app(scope, receive, send)
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse({"detail": "仅允许本机与 Tailscale 私有网络访问"}, status_code=403)
            await response(scope, receive, send)


async def periodic(label, interval, action):
    while True:
        try:
            await asyncio.to_thread(action)
        except Exception as error:
            logger.info("%s 本轮未完成（%s）", label, type(error).__name__)
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, data_dir: Path = ROOT / "data", static_dir: Path = ROOT / "backend" / "static",
               config_path: Path | None = None, interactive_media: bool = False) -> FastAPI:
    if settings is None:
        config_path = config_path or ROOT / "config.json"
        settings = load_settings(config_path)
    identity = host_identity()
    macos = identity["platform"] == "macos"
    store = Scratchpad(data_dir)
    wallpaper_lock = Lock()
    backups = Backups(store, data_dir, wallpaper_lock)
    health = HealthStore(store.connect, settings.health)
    calendar = CalendarService(settings.calendar, health.zone)
    life = LifeStore(store.connect, health.zone)
    agent_auth = require_agent_token(settings.agents)
    agents = AgentRegistry(store.connect)
    rates = Rates(settings.finance)
    expenses = ExpenseStore(store.connect, health.zone, rates)
    rate_pins = RatePins(store.connect, settings.finance)
    rate_history = RateHistory(rates, store.connect)
    sensor_file = data_dir / "fancontrol-mode.sensor" if settings.fancontrol_mode_sensor else None
    power = MacPowerControl(settings) if macos else PowerControl(settings, sensor_file=sensor_file)
    weather = Weather(settings.weather)
    ai_state = AiState(settings.ai, config_path)
    usage_ledger = UsageLedger(store.connect)
    briefing = DailyBriefing(store, health, weather, calendar, usage_ledger)
    if interactive_media and identity["platform"] == "windows":
        from .media_desktop import InteractiveMediaBridge

        media = InteractiveMediaBridge()
        media_session = media.session
    else:
        media = MacMediaBridge() if macos else MediaBridge()
        media_session = nullcontext
    ai_tools = AiTools(life, health, calendar, rates, store, power, media, agents)
    deck = DeckStore(store.connect)
    plugin_path = (config_path.parent if config_path else ROOT) / "plugins"
    plugins = DeckPluginRegistry(plugin_path, settings.deck.enabled_plugins)
    deck_actions = DeckActions(settings, config_path, identity["platform"], agents, power, media, plugins)
    plugin_manager = DeckPluginManager(plugins, data_dir, ROOT / "plugins", store.connect)

    def initialize_data():
        with backups.restore_on_startup():
            store.initialize()
            health.initialize()
            life.initialize()
            expenses.initialize()
            rate_pins.initialize()
            rate_history.initialize()
            usage_ledger.initialize()
            deck.initialize()
            plugin_manager.initialize()
            agents.restore_profiles()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with backups.session(), media_session():
            await asyncio.to_thread(initialize_data)
            monitor = Monitor(settings, identity)
            app.state.monitor = monitor
            async with (httpx.AsyncClient(trust_env=False) as lhm_client, httpx.AsyncClient() as external_client,
                        httpx.AsyncClient(trust_env=False, timeout=4.5) as media_client):
                app.state.external_client = external_client
                app.state.media_client = media_client
                workers = [
                    asyncio.create_task(monitor.run(lhm_client, lambda: power.latest), name="metrics"),
                    asyncio.create_task(periodic("电源计划查询", 15, power.active_power), name="power"),
                    asyncio.create_task(periodic("中转站清理", 3600, store.cleanup), name="cleanup"),
                    asyncio.create_task(periodic("Agent 快照保存", PROFILE_INTERVAL, agents.flush_profiles), name="agent-profiles"),
                    asyncio.create_task(rate_history.sample(external_client, rate_pins), name="rate-snapshots"),
                ]
                if health.export_dir is not None:
                    workers.append(asyncio.create_task(periodic("本地健康导入", 120, health.scan), name="health-import"))
                if calendar.sources:
                    workers.append(asyncio.create_task(calendar.run(external_client), name="calendar-sync"))
                if settings.webhook.kind != "disabled":
                    workers.append(asyncio.create_task(monitor.send_notifications(external_client), name="webhook"))
                if settings.webhook.kind == "bark":
                    workers.append(asyncio.create_task(expenses.run_bark(external_client, settings.webhook), name="subscription-bark"))
                if settings.webhook.kind == "bark" and settings.webhook.daily_briefing_time:
                    workers.append(asyncio.create_task(briefing.run_bark(external_client, ai_state, settings.webhook), name="daily-briefing-bark"))
                try:
                    yield
                finally:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    await ai_tools.close()
                    await power.close()
                    await asyncio.to_thread(agents.flush_profiles)

    app = FastAPI(title="Vitals", lifespan=lifespan, redoc_url=None)
    app.state.host_platform = identity["platform"]
    app.add_middleware(PrivateNetwork)
    if config_path is not None:
        app.include_router(setup_router(config_path, settings))
    app.include_router(control_router(power))
    app.include_router(launchers_router(config_path, settings.launchers, identity["platform"]))
    app.include_router(screenshot_router(agents, agent_auth, identity["platform"]))
    app.include_router(scratchpad_router(store, settings.upload_limit_mib * MIB))
    app.include_router(wallpaper_router(data_dir, wallpaper_lock))
    app.include_router(backup_router(backups, identity))
    app.include_router(updates_router(identity["platform"], static_dir / "index.html"))
    app.include_router(health_router(health))
    app.include_router(weather_router(weather))
    app.include_router(calendar_router(calendar))
    app.include_router(agents_router(agents, agent_auth))
    app.include_router(life_router(life, agent_auth))
    app.include_router(rates_router(rates, rate_pins, rate_history))
    app.include_router(expenses_router(expenses))
    app.include_router(ai_router(ai_state, store, power, briefing, ai_tools))
    app.include_router(usage_router(AiUsage(usage_ledger, settings.ai_usage, health.zone)))
    app.include_router(media_router(media, LyricsCache(store), agents))
    app.include_router(deck_router(deck, deck_actions))
    app.include_router(deck_icons_router(data_dir))
    app.include_router(deck_plugin_router(
        plugin_manager, lambda request: direct_setup_request(request.scope, request.headers, http_origin),
    ))

    sampling_write_lock = asyncio.Lock()

    @app.get("/api/system/sampling")
    async def get_sampling(response: Response) -> SamplingPreference:
        response.headers["Cache-Control"] = "no-store"
        monitor = getattr(app.state, "monitor", None)
        mode = settings.sampling_mode if monitor is None else monitor.sampling_mode
        return SamplingPreference(mode=mode)

    @app.put("/api/system/sampling")
    async def update_sampling(preference: SamplingPreference) -> SamplingPreference:
        if config_path is None:
            raise HTTPException(409, "此服务未指定配置文件，无法保存采样模式")
        monitor = getattr(app.state, "monitor", None)
        if monitor is None:
            raise HTTPException(503, "硬件监控尚未启动")
        async with sampling_write_lock:
            try:
                await run_in_threadpool(update_settings_file, config_path,
                                        lambda document, _: document.update(sampling_mode=preference.mode))
            except (OSError, ValueError):
                raise HTTPException(409, "无法保存采样模式，请检查服务端配置文件后重试") from None
            monitor.set_sampling_mode(preference.mode)
        return preference

    @app.websocket("/ws/metrics")
    async def metrics(websocket: WebSocket):
        monitor: Monitor = websocket.app.state.monitor
        await websocket.accept()
        queue = monitor.subscribe()

        async def send_snapshots():
            while True:
                snapshot = await queue.get()
                await asyncio.wait_for(websocket.send_json(snapshot), timeout=5)

        async def receive_client_states():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload = message.get("text")
                if not payload or len(payload) > 256:
                    continue
                try:
                    state = MetricsClientState.model_validate_json(payload)
                except ValidationError:
                    continue
                monitor.set_client_state(queue, visible=state.visible, active_telemetry=state.active_telemetry)

        tasks = [asyncio.create_task(send_snapshots()), asyncio.create_task(receive_client_states())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            results = await asyncio.gather(*done, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, DISCONNECT_ERRORS + (asyncio.TimeoutError,)):
                    raise result
            if any(isinstance(result, asyncio.TimeoutError) for result in results):
                # 半开连接的关闭帧也可能卡住，最多等待两秒。
                with suppress(Exception):
                    await asyncio.wait_for(websocket.close(code=1013), timeout=2)
        finally:
            monitor.unsubscribe(queue)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    else:
        @app.get("/", include_in_schema=False)
        def index():
            return {"service": "Vitals", "docs": "/docs"}

    return app


@contextmanager
def bound_sockets(host: str, port: int, desktop: bool = False):
    # 先占用端口，再启动数据库和后台任务。Windows 需要独占绑定以阻止通配地址与指定接口重复监听。
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if desktop:
        bound_hosts = {address[0] for *_, address in addresses}
        for loopback, wildcard in (("127.0.0.1", "0.0.0.0"), ("::1", "::")):
            if not {loopback, wildcard} & bound_hosts:
                addresses.extend(socket.getaddrinfo(loopback, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
    with ExitStack() as resources:
        listeners = []
        for family, kind, protocol, _, address in addresses:
            listener = resources.enter_context(socket.socket(family, kind, protocol))
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            option = getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR)
            listener.setsockopt(socket.SOL_SOCKET, option, 1)
            listener.bind(address)
            listener.listen()
            listeners.append(listener)
        yield listeners


def desktop_access_errors(record: logging.LogRecord) -> bool:
    # Uvicorn access records carry peer, method, path, protocol and status.
    if isinstance(record.args, tuple) and len(record.args) == 5:
        peer, method, path, protocol, status = record.args
        if isinstance(status, int):
            if status < 400:
                return False
            record.args = (peer, method, str(path).partition("?")[0], protocol, status)
    return True


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Start the Vitals hub")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--desktop-stdio", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.host is not None or args.port is not None:
        settings = Settings.model_validate({**settings.model_dump(),
            **{key: value for key, value in {"host": args.host, "port": args.port}.items() if value is not None}})
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with ExitStack() as resources:
        try:
            listeners = resources.enter_context(bound_sockets(settings.host, settings.port, desktop=args.desktop_stdio))
        except OSError as error:
            logger.error("Vitals 无法绑定 %s:%s（%s）", settings.host, settings.port, error)
            raise SystemExit(1) from None
        config = uvicorn.Config(
            create_app(settings, data_dir=args.data_dir.resolve(), config_path=args.config.resolve(),
                       interactive_media=args.desktop_stdio), host=settings.host, port=settings.port,
            workers=1, proxy_headers=False, ws_max_size=16 * 1024, ws_ping_interval=20, ws_ping_timeout=20,
            timeout_graceful_shutdown=5 if args.desktop_stdio else None,
        )
        if args.desktop_stdio:
            logging.getLogger("uvicorn.access").addFilter(desktop_access_errors)
        logger.info("Vitals 正在监听 %s:%s", settings.host, settings.port)
        server = uvicorn.Server(config)
        if args.desktop_stdio:
            # The tray owns the write end. EOF also shuts down the hub if the tray crashes.
            def wait_for_desktop():
                os.read(sys.stdin.fileno(), 1)
                server.should_exit = True

            Thread(target=wait_for_desktop, daemon=True, name="desktop-lifetime").start()
        server.run(sockets=listeners)


if __name__ == "__main__":
    main()
elif not getattr(sys, "frozen", False):
    app = create_app()
