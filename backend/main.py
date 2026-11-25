"""FastAPI 入口与 Uvicorn 启动配置。"""

import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
import ipaddress
import logging
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from websockets.exceptions import ConnectionClosed

from .ai import AiState, DailyBriefing, ai_router
from .config import MIB, ROOT, Settings, load_settings
from .control import PowerControl, control_router
from .health import HealthStore, health_router
from .lyrics import LyricsCache
from .media import MediaBridge, media_router
from .monitor import Monitor
from .scratchpad import Scratchpad, scratchpad_router
from .wallpaper import wallpaper_router
from .weather import Weather, weather_router


TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HTTP_AUTHORITY = re.compile(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?")
DISCONNECT_ERRORS = (WebSocketDisconnect, RuntimeError, OSError, ConnectionClosed)
logger = logging.getLogger(__name__)


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
        allowed = bool(peer and local_address(peer[0]) and trusted_host(host))
        if allowed and (scope_type == "websocket" or scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                        or scope.get("path", "").rstrip("/") in {"/api/ai/briefing", "/api/media/lyrics"}):
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
               config_path: Path | None = None) -> FastAPI:
    if settings is None:
        config_path = config_path or ROOT / "config.json"
        settings = load_settings(config_path)
    store = Scratchpad(data_dir)
    health = HealthStore(store.connect, settings.health)
    sensor_file = data_dir / "fancontrol-mode.sensor" if settings.fancontrol_mode_sensor else None
    power = PowerControl(settings, sensor_file=sensor_file)
    weather = Weather(settings.weather)
    ai_state = AiState(settings.ai, config_path)
    briefing = DailyBriefing(store, health, weather)
    media = MediaBridge()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(store.initialize)
        await asyncio.to_thread(health.initialize)
        monitor = Monitor(settings)
        app.state.monitor = monitor
        async with (httpx.AsyncClient(trust_env=False) as lhm_client, httpx.AsyncClient() as external_client,
                    httpx.AsyncClient(trust_env=False, timeout=4.5) as media_client):
            app.state.external_client = external_client
            app.state.media_client = media_client
            workers = [
                asyncio.create_task(monitor.run(lhm_client, lambda: power.latest), name="metrics"),
                asyncio.create_task(periodic("电源计划查询", 15, power.active_power), name="power"),
                asyncio.create_task(periodic("中转站清理", 3600, store.cleanup), name="cleanup"),
            ]
            if health.export_dir is not None:
                workers.append(asyncio.create_task(periodic("本地健康导入", 120, health.scan), name="health-import"))
            if settings.webhook.kind != "disabled":
                workers.append(asyncio.create_task(monitor.send_notifications(external_client), name="webhook"))
            if settings.webhook.kind == "bark" and settings.webhook.daily_briefing_time:
                workers.append(asyncio.create_task(briefing.run_bark(external_client, ai_state, settings.webhook), name="daily-briefing-bark"))
            try:
                yield
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                await power.close()

    app = FastAPI(title="Vitals", lifespan=lifespan, redoc_url=None)
    app.add_middleware(PrivateNetwork)
    app.include_router(control_router(power))
    app.include_router(scratchpad_router(store, settings.upload_limit_mib * MIB))
    app.include_router(wallpaper_router(data_dir))
    app.include_router(health_router(health))
    app.include_router(weather_router(weather))
    app.include_router(ai_router(ai_state, store, power, briefing))
    app.include_router(media_router(media, LyricsCache(store)))

    @app.websocket("/ws/metrics")
    async def metrics(websocket: WebSocket):
        monitor: Monitor = websocket.app.state.monitor
        queue = asyncio.Queue(maxsize=1)
        await websocket.accept()
        monitor.subscribers.add(queue)
        if monitor.latest is not None:
            queue.put_nowait(monitor.latest)

        async def send_snapshots():
            while True:
                snapshot = await queue.get()
                await asyncio.wait_for(websocket.send_json(snapshot), timeout=5)

        async def wait_for_disconnect():
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(send_snapshots()), asyncio.create_task(wait_for_disconnect())]
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
            monitor.subscribers.discard(queue)
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
def bound_sockets(host: str, port: int):
    # 先占用端口，再启动数据库和后台任务。Windows 需要独占绑定以阻止通配地址与指定接口重复监听。
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
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


def main():
    import uvicorn

    settings = load_settings()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with ExitStack() as resources:
        try:
            listeners = resources.enter_context(bound_sockets(settings.host, settings.port))
        except OSError as error:
            logger.error("Vitals 无法绑定 %s:%s（%s）", settings.host, settings.port, error)
            raise SystemExit(1) from None
        config = uvicorn.Config(
            create_app(settings, config_path=ROOT / "config.json"), host=settings.host, port=settings.port,
            workers=1, proxy_headers=False, ws_max_size=16 * 1024, ws_ping_interval=20, ws_ping_timeout=20,
        )
        logger.info("Vitals 正在监听 %s:%s", settings.host, settings.port)
        uvicorn.Server(config).run(sockets=listeners)


if __name__ == "__main__":
    main()
else:
    app = create_app()
