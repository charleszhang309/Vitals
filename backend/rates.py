"""Frankfurter 参考汇率和跨设备固定列表。"""

import asyncio
from collections import defaultdict
from datetime import date
import json
import math
from time import monotonic
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from .rate_history import HistoryRange


CURRENCIES = ("CAD", "CNY", "USD", "EUR", "GBP", "JPY", "HKD", "AUD", "NZD", "CHF", "SGD", "KRW", "INR", "SEK", "NOK", "TWD", "MXN", "BRL")
MAX_PINS = 5


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: str = Field(pattern=r"^[A-Z]{3}$")
    target: str = Field(pattern=r"^[A-Z]{3}$")


class MovePin(BaseModel):
    direction: Literal["up", "down"]


class RatePins:
    def __init__(self, connect, config):
        self.connect = connect
        self.defaults = [{"base": config.base_currency, "target": code} for code in config.target_currencies
                         if code != config.base_currency][:MAX_PINS]

    def initialize(self):
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS rate_pins (id INTEGER PRIMARY KEY CHECK(id=1), pairs TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO rate_pins(id,pairs) VALUES (1,?)", (json.dumps(self.defaults),))

    def list(self):
        with self.connect() as db:
            return json.loads(db.execute("SELECT pairs FROM rate_pins WHERE id=1").fetchone()[0])

    def mutate(self, pair, action):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pairs = json.loads(db.execute("SELECT pairs FROM rate_pins WHERE id=1").fetchone()[0])
            if action == "add":
                if pair not in pairs:
                    if len(pairs) == MAX_PINS:
                        raise HTTPException(409, "最多固定 5 组汇率")
                    pairs.append(pair)
            else:
                if pair not in pairs:
                    raise HTTPException(404, "没有固定这组汇率")
                index = pairs.index(pair)
                if action == "delete":
                    pairs.pop(index)
                else:
                    neighbor = index + (-1 if action == "up" else 1)
                    if 0 <= neighbor < len(pairs):
                        pairs[index], pairs[neighbor] = pairs[neighbor], pairs[index]
            db.execute("UPDATE rate_pins SET pairs=? WHERE id=1", (json.dumps(pairs),))
            return pairs


class Rates:
    def __init__(self, config):
        self.base = config.base_currency
        self.targets = config.target_currencies
        self.currencies = tuple(dict.fromkeys((*CURRENCIES, self.base, *self.targets)))
        self.locks = defaultdict(asyncio.Lock)
        self.cache = {}
        self.expires = {}

    def validate(self, base, target):
        if base not in self.currencies or target not in self.currencies:
            raise HTTPException(422, "不支持所选币种")

    async def quotes(self, client, base, targets):
        if any(code != base and monotonic() >= self.expires.get((base, code), 0) for code in targets):
            async with self.locks[base]:
                due = [code for code in targets if code != base and monotonic() >= self.expires.get((base, code), 0)]
                if due:
                    await self.fetch(client, base, due)
        return {code: ({"rate": 1, "date": None, "status": "live"} if code == base else self.cache[(base, code)]) for code in targets}

    async def fetch(self, client, base, targets):
        fresh = {}
        try:
            reply = await client.get("https://api.frankfurter.dev/v2/rates",
                                     params={"base": base, "quotes": ",".join(targets)}, timeout=6)
            reply.raise_for_status()
            for entry in reply.json():
                code, value = entry["quote"], float(entry["rate"])
                if code in targets and entry["base"] == base and value > 0 and math.isfinite(value):
                    fresh[code] = {"rate": value, "date": date.fromisoformat(entry["date"]).isoformat(), "status": "live"}
        except (httpx.HTTPError, KeyError, ValueError, TypeError, OverflowError):
            pass
        for code in targets:
            key = (base, code)
            previous = self.cache.get(key, {})
            self.cache[key] = fresh.get(code) or {"rate": previous.get("rate"), "date": previous.get("date"),
                                                 "status": "stale" if previous.get("rate") is not None else "unavailable"}
            self.expires[key] = monotonic() + (3600 if code in fresh else 60)

    async def read(self, client):
        quotes = await self.quotes(client, self.base, self.targets)
        statuses = {item["status"] for code, item in quotes.items() if code != self.base} or {"live"}
        status = "live" if "live" in statuses else "stale" if "stale" in statuses else "unavailable"
        return {"status": status, "base_currency": self.base, "target_currencies": self.targets,
                "rates": {code: {"rate": item["rate"], "date": item["date"]}
                          for code, item in quotes.items() if item["rate"] is not None}}

    async def pinned(self, client, pairs):
        groups = defaultdict(list)
        for pair in pairs:
            groups[pair["base"]].append(pair["target"])
        values = await asyncio.gather(*(self.quotes(client, base, targets) for base, targets in groups.items()))
        by_base = dict(zip(groups, values))
        return [{**pair, **by_base[pair["base"]][pair["target"]]} for pair in pairs]


def rates_router(rates, pins, history):
    router = APIRouter(prefix="/api/rates", tags=["rates"])

    @router.get("")
    async def read(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        pairs = await run_in_threadpool(pins.list)
        client = request.app.state.external_client
        ordered, configured = await asyncio.gather(rates.pinned(client, pairs), rates.read(client))
        return {**configured, "pins": ordered, "currencies": rates.currencies, "max_pins": MAX_PINS}

    @router.get("/quote")
    async def quote(base: str, target: str, request: Request):
        rates.validate(base, target)
        result = await rates.quotes(request.app.state.external_client, base, [target])
        return {"base": base, "target": target, **result[target]}

    @router.get("/history")
    async def history_series(base: str, target: str, request: Request, range: HistoryRange = "7d"):
        return await history.read(request.app.state.external_client, base, target, range)

    @router.post("/pins")
    def add_pin(pair: Pair):
        rates.validate(pair.base, pair.target)
        if pair.base == pair.target:
            raise HTTPException(422, "请选择两个不同的币种")
        return {"pins": pins.mutate(pair.model_dump(), "add")}

    @router.patch("/pins/{base}/{target}")
    def move_pin(base: str, target: str, update: MovePin):
        return {"pins": pins.mutate({"base": base, "target": target}, update.direction)}

    @router.delete("/pins/{base}/{target}")
    def delete_pin(base: str, target: str):
        return {"pins": pins.mutate({"base": base, "target": target}, "delete")}

    return router
