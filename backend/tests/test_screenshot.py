import asyncio
from io import BytesIO
from unittest.mock import Mock
from uuid import uuid4

import httpx
from fastapi import FastAPI
from PIL import Image

from backend.agents import AgentRegistry, agents_router, require_agent_token
from backend.config import Settings
from backend.main import PrivateNetwork
from backend.screenshot import MAX_BYTES, MAX_WIDTH, screenshot_router


TOKEN = {"Authorization": "Bearer fixture-agent-token"}
AGENT = "/api/agents/macbook"
CAPTURE = "/api/control/screenshot"
SNAPSHOT = {"name": "MacBook", "os": "macos", "timestamp": "2026-01-01T00:00:00Z", "capabilities": ["screenshot"]}


def make_client(platform="windows"):
    registry = AgentRegistry()
    authenticate = require_agent_token(Settings(agents={"token": "fixture-agent-token"}).agents)
    app = FastAPI()
    app.add_middleware(PrivateNetwork)
    app.include_router(agents_router(registry, authenticate))
    app.include_router(screenshot_router(registry, authenticate, platform))
    return registry, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def test_windows_returns_primary_display_with_no_store(monkeypatch):
    grab = Mock(return_value=Image.new("RGB", (2880, 1800)))
    monkeypatch.setattr("backend.screenshot.ImageGrab.grab", grab)

    async def verify():
        _, client = make_client()
        async with client:
            assert (await client.post(CAPTURE, headers={"Origin": "https://outside.example"})).status_code == 403
            grab.assert_not_called()
            response = await client.post(CAPTURE)
        grab.assert_called_once_with(all_screens=False)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        with Image.open(BytesIO(response.content)) as image:
            assert image.format in ("WEBP", "JPEG")
            assert image.size == (1440, 900)

    asyncio.run(verify())


async def pending_command(client, registry):
    request = asyncio.create_task(client.post(CAPTURE + "/macbook"))
    await asyncio.sleep(0)
    (command_id,) = registry.devices["macbook"].commands
    return request, command_id, f"{AGENT}/commands/{command_id}/screenshot"


def test_remote_only_accepts_upload_for_active_delivered_command():
    image = BytesIO()
    Image.new("RGB", (25, 20)).save(image, "JPEG")
    body = image.getvalue()
    image_headers = {**TOKEN, "Content-Type": "image/jpeg"}

    async def verify():
        registry, client = make_client()
        async with client:
            assert (await client.post(AGENT + "/heartbeat", headers=TOKEN, json=SNAPSHOT)).status_code == 200
            assert (await client.post(AGENT + "/command", json={"action": "screenshot"})).status_code == 422
            request, command_id, route = await pending_command(client, registry)
            assert (await client.post(CAPTURE + "/macbook")).status_code == 409
            assert (await client.post(route + "/start", headers=TOKEN)).status_code == 404
            delivered = await client.post(AGENT + "/heartbeat", headers=TOKEN, json=SNAPSHOT)
            assert delivered.json()["commands"] == [{"id": str(command_id), "action": "screenshot", "payload": {}}]
            assert not (await client.get("/api/agents")).json()["devices"][0]["commands"]
            assert (await client.post(route + "/start")).status_code == 401
            assert (await client.post(route, content=body, headers=image_headers)).status_code == 409
            assert (await client.post(route + "/start", headers=TOKEN)).status_code == 204
            assert (await client.post(route + "/start", headers=TOKEN)).status_code == 409
            for wrong_route in (route.replace("macbook", "elsewhere"), route.replace(str(command_id), str(uuid4()))):
                assert (await client.post(wrong_route, headers=image_headers, content=body)).status_code == 404
            assert (await client.post(route, headers=image_headers, content=b"not an image")).status_code == 422
            assert (await client.post(route, headers={**image_headers, "Content-Length": str(MAX_BYTES + 1)})).status_code == 413
            too_wide = BytesIO()
            Image.new("RGB", (MAX_WIDTH + 1, 10)).save(too_wide, "JPEG")
            assert (await client.post(route, headers=image_headers, content=too_wide.getvalue())).status_code == 422
            assert (await client.post(route, headers=image_headers, content=body)).status_code == 204
            response = await request
            assert response.content == body
            assert response.headers["cache-control"] == "no-store"
            assert (await client.post(route, headers=image_headers, content=body)).status_code == 404
            assert not registry.devices["macbook"].commands

    asyncio.run(verify())


def test_remote_permission_and_timeout_remove_the_pending_command(monkeypatch):
    monkeypatch.setattr("backend.screenshot.CAPTURE_TIMEOUT", 0.25)

    async def verify():
        registry, client = make_client()
        async with client:
            await client.post(AGENT + "/heartbeat", headers=TOKEN, json=SNAPSHOT)
            request, _, route = await pending_command(client, registry)
            await client.post(AGENT + "/heartbeat", headers=TOKEN, json=SNAPSHOT)
            assert (await client.post(route + "/start", headers=TOKEN)).status_code == 204
            assert (await client.post(route + "/error/permission", headers=TOKEN)).status_code == 204
            denied = await request
            assert denied.status_code == 403
            assert denied.json()["detail"] == "请给 Vitals 授予录屏权限，然后重启 Vitals。"
            assert not registry.devices["macbook"].commands
            request, _, route = await pending_command(client, registry)
            assert (await request).status_code == 504
            assert not registry.devices["macbook"].commands
            assert (await client.post(route + "/start", headers=TOKEN)).status_code == 404

    asyncio.run(verify())
