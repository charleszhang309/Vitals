"""The hub-owned Deck layout, optimistic revisions, and runtime API."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from .agents import DEVICE_PATTERN
from .deck_actions import DeckActions, execute_macro, parameters
from .deck_icons import MAX_ICON_VALUE_LENGTH, validate_icon_value


GRIDS = {"3x2": 6, "4x3": 12, "5x3": 15, "6x3": 18}
PALETTES = ("slate", "blue", "teal", "green", "amber", "pink", "purple", "red")
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,48}$")]
DeviceKey = Annotated[str, StringConstraints(pattern=DEVICE_PATTERN)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Action(Model):
    id: Key
    params: dict = Field(default_factory=dict, max_length=24)

    @model_validator(mode="after")
    def valid_parameters(self):
        parameters(self.id, self.params)
        return self


class Step(Model):
    action: Action
    delay_ms: int = Field(default=0, ge=0, le=3000)


class Feedback(Model):
    plugin_id: Key
    feedback_id: Key | None = None
    variable_id: Key | None = None
    args: dict = Field(default_factory=dict, max_length=24)

    @model_validator(mode="after")
    def selector(self):
        if (self.feedback_id is None) == (self.variable_id is None) or (self.variable_id and self.args):
            raise ValueError("请选择一个插件状态或变量")
        return self


class Tile(Model):
    kind: Literal["action", "toggle", "live", "folder"] = "action"
    title: str = Field(default="", max_length=40)
    icon: str = Field(default="", max_length=MAX_ICON_VALUE_LENGTH)
    color: Literal[PALETTES] = "slate"
    span: Literal[1, 2] = 1
    steps: list[Step] = Field(default_factory=list, max_length=8)
    page: Key | None = None
    live: Literal["cpu", "gpu", "focus", "ai"] | None = None
    feedback: Feedback | None = None

    @field_validator("icon")
    @classmethod
    def check_icon(cls, icon: str) -> str:
        return validate_icon_value(icon)

    @field_validator("span", mode="before")
    @classmethod
    def check_span(cls, span):
        if type(span) is not int:
            raise ValueError("Deck 按键宽度无效")
        return span

    @model_validator(mode="after")
    def check_kind(self):
        if self.kind in {"action", "toggle"}:
            valid = bool(self.steps) and not self.page and not self.live and (self.kind == "toggle" or not self.feedback)
        elif self.kind == "folder":
            valid = bool(self.page) and not (self.steps or self.live or self.feedback)
        else:
            valid = not (self.steps or self.page) and bool(self.live) != bool(self.feedback)
        if not valid or sum(step.delay_ms for step in self.steps) > 10_000:
            raise ValueError("Deck 按键配置无效")
        return self


class Scene(Model):
    id: Key
    title: str = Field(min_length=1, max_length=32)
    grid: Literal[tuple(GRIDS)] = "4x3"
    folder: bool = False
    tiles: list[Tile | None] = Field(default_factory=list, max_length=18)

    @model_validator(mode="after")
    def capacity(self):
        if len(self.tiles) > GRIDS[self.grid] or not self.title.strip():
            raise ValueError("Deck 场景配置无效")
        columns = int(self.grid[0])
        for index, tile in enumerate(self.tiles):
            if tile and tile.span == 2 and ((index + 1) % columns == 0 or
                                           (index + 1 < len(self.tiles) and self.tiles[index + 1])):
                raise ValueError("宽键需要同一行右侧有空位")
        return self


class Layout(Model):
    version: Literal[1] = 1
    scenes: list[Scene] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def scene_links(self):
        ids = {scene.id for scene in self.scenes}
        if len(ids) != len(self.scenes) or all(scene.folder for scene in self.scenes):
            raise ValueError("Deck 至少需要一个独立场景")
        if any(tile.page not in ids or tile.page == scene.id
               for scene in self.scenes for tile in scene.tiles if tile and tile.kind == "folder"):
            raise ValueError("Deck 文件夹链接无效")
        return self

    def tile(self, scene_id, slot):
        for scene in self.scenes:
            if scene.id == scene_id and 0 <= slot < len(scene.tiles) and scene.tiles[slot]:
                return scene.tiles[slot]
        raise HTTPException(404, "找不到该 Deck 按键")


def default_layout():
    def action(title, icon, identifier, args=None, kind="action", color="slate"):
        return Tile(kind=kind, title=title, icon=icon, color=color, steps=[Step(action=Action(id=identifier, params=args or {}))])

    return Layout(scenes=[
        Scene(id="daily", title="日常", grid="3x2", tiles=[
            action("", "🔈", "mute", kind="toggle", color="blue"),
            action("", "⏱", "focus", {"action": "toggle"}, kind="toggle", color="teal"),
            action("", "▣", "screenshot", color="slate"),
            Tile(kind="live", live="cpu", icon="🌡", color="amber"),
            Tile(kind="live", live="ai", icon="✦", color="purple"),
            Tile(kind="folder", page="desktop", icon="▦", color="blue"),
        ]),
        Scene(id="media", title="媒体", grid="3x2", tiles=[
            action("", "⏮", "media", {"action": "previous"}),
            action("", "⏯", "media", {"action": "toggle"}, kind="toggle", color="blue"),
            action("", "⏭", "media", {"action": "next"}),
            action("", "−", "volume", {"step": -10}),
            action("", "+", "volume", {"step": 10}),
            Tile(kind="live", live="gpu", icon="🌡", color="amber"),
        ]),
        Scene(id="desktop", title="工作", grid="3x2", folder=True, tiles=[
            action("", "←", "preset", {"preset": "desktop_left"}),
            action("", "→", "preset", {"preset": "desktop_right"}),
            action("", "◉", "desktop", {"action": "display_off"}),
        ]),
    ])


class WriteLayout(Model):
    revision: int = Field(ge=0)
    layout: Layout


class Selection(Model):
    revision: int = Field(ge=0)
    scene: Key
    slot: int = Field(ge=0, lt=18)


class Run(Selection):
    target: DeviceKey = "hub"
    step: int | None = Field(default=None, ge=0, lt=8)


class FeedbackRequest(Model):
    revision: int = Field(ge=0)
    scene: Key
    slots: list[Annotated[int, Field(ge=0, lt=18)]] = Field(max_length=18)


class FaderChange(Model):
    percent: int | None = Field(default=None, ge=0, le=100)
    muted: StrictBool | None = None
    input_percent: int | None = Field(default=None, ge=0, le=100)
    input_muted: StrictBool | None = None

    @model_validator(mode="after")
    def has_change(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少设置一项音频控制")
        return self


class DeckStore:
    def __init__(self, connect):
        self.connect = connect
        self.condition = asyncio.Condition()
        self.notified_revision = 0

    def initialize(self):
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deck_layout (
                id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL, document TEXT NOT NULL
            )""")
            db.execute("INSERT OR IGNORE INTO deck_layout VALUES (1, 0, ?)", (default_layout().model_dump_json(),))

    def read(self):
        with self.connect() as db:
            revision, document = db.execute("SELECT revision, document FROM deck_layout WHERE id=1").fetchone()
        return {"revision": revision, "layout": Layout.model_validate_json(document)}

    def write(self, payload):
        document = payload.layout.model_dump_json()
        if len(document.encode()) > 256 * 1024:
            raise HTTPException(413, "Deck 布局超过大小限制")
        with self.connect() as db:
            changed = db.execute("UPDATE deck_layout SET revision=revision+1, document=? WHERE id=1 AND revision=?",
                                 (document, payload.revision)).rowcount
        if not changed:
            raise HTTPException(409, "Deck 已在另一台设备修改，请重新载入后再保存")
        return {"revision": payload.revision + 1, "layout": payload.layout}

    async def publish(self, revision):
        async with self.condition:
            self.notified_revision = max(self.notified_revision, revision)
            self.condition.notify_all()

    async def events(self):
        revision = (await asyncio.to_thread(self.read))["revision"]
        yield f"data: {revision}\n\n"
        while True:
            try:
                async with self.condition:
                    await asyncio.wait_for(self.condition.wait_for(lambda: self.notified_revision > revision), 20)
                    revision = self.notified_revision
                yield f"data: {revision}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"


def deck_router(store: DeckStore, actions: DeckActions):
    router = APIRouter(prefix="/api/deck", tags=["deck"])
    executing = set()

    def current(revision):
        loaded = store.read()
        if loaded["revision"] != revision:
            raise HTTPException(409, "Deck 已在另一台设备修改，请重新载入后再保存")
        return loaded["layout"]

    @router.get("")
    def layout(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.read()

    @router.put("")
    async def save(payload: WriteLayout):
        result = await asyncio.to_thread(store.write, payload)
        await store.publish(result["revision"])
        return result

    @router.post("/validate")
    def validate(document: Layout):
        if len(document.model_dump_json().encode()) > 256 * 1024:
            raise HTTPException(413, "Deck 布局超过大小限制")
        return document

    @router.get("/events")
    async def events():
        return StreamingResponse(store.events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/catalog")
    async def catalog(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await actions.catalog()

    @router.get("/audio")
    async def audio(target: DeviceKey, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        _, snapshot = actions.target(target)
        return await actions.audio(request.app.state.media_client, snapshot)

    @router.post("/audio")
    async def set_audio(change: FaderChange, request: Request, target: DeviceKey = "hub"):
        volume = await actions.set_faders(target, request.app.state.media_client, change.model_dump(exclude_none=True))
        return Response(status_code=204) if volume is None else volume

    @router.post("/run")
    async def run(payload: Run, request: Request):
        actions.target(payload.target)
        if payload.target in executing:
            raise HTTPException(409, "该设备正在执行 Deck 动作")
        executing.add(payload.target)
        try:
            loaded = await asyncio.to_thread(current, payload.revision)
            tile = loaded.tile(payload.scene, payload.slot)
            steps = tile.steps
            if not steps or (payload.step is not None and payload.step >= len(steps)):
                raise HTTPException(422, "找不到该 Deck 动作")
            if payload.step is not None:
                steps = [steps[payload.step]]
            client = request.app.state.media_client
            try:
                result = await execute_macro(steps, lambda action: actions.invoke(action, payload.target, client))
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(409, str(error) if type(error) in {ValueError, RuntimeError} else "Deck 动作执行失败") from None
            return {"results": result}
        finally:
            executing.discard(payload.target)

    @router.post("/feedback")
    async def feedback(payload: FeedbackRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        loaded = await asyncio.to_thread(current, payload.revision)
        values, variables = {}, {}
        for slot in dict.fromkeys(payload.slots):
            spec = loaded.tile(payload.scene, slot).feedback
            if not spec:
                continue
            try:
                if spec.variable_id:
                    if spec.plugin_id not in variables:
                        variables[spec.plugin_id] = await actions.plugin_call(actions.plugins.get_variables, spec.plugin_id)
                    value = variables[spec.plugin_id].get(spec.variable_id)
                else:
                    value = await actions.plugin_call(actions.plugins.get_state, spec.plugin_id, spec.feedback_id, spec.args)
                values[slot] = value
            except HTTPException:
                values[slot] = None
        return {"values": values}

    return router
