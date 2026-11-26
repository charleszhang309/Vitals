"""日参考汇率与本地小时快照。"""

import asyncio
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
import logging
import math
from time import monotonic, time
from typing import Literal

import httpx


HistoryRange = Literal["24h", "7d", "30d", "1y"]
HISTORY_DAYS = {"7d": 7, "30d": 30, "1y": 365}
HOUR = 3600
logger = logging.getLogger(__name__)


class RateHistory:
    def __init__(self, rates, connect):
        self.rates = rates
        self.connect = connect
        self.cache = OrderedDict()
        self.locks = defaultdict(asyncio.Lock)

    def initialize(self):
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS rate_snapshots (
                base TEXT NOT NULL, target TEXT NOT NULL, hour INTEGER NOT NULL, rate REAL NOT NULL,
                PRIMARY KEY (base, target, hour)
            )""")

    def store(self, hour, quotes):
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO rate_snapshots VALUES (?,?,?,?)", [
                (item["base"], item["target"], hour, item["rate"])
                for item in quotes if item["status"] == "live" and item["base"] != item["target"]
            ])
            db.execute("DELETE FROM rate_snapshots WHERE hour < ?", (hour - 48 * HOUR,))

    def snapshots(self, base, target, now):
        with self.connect() as db:
            return [{"time": datetime.fromtimestamp(hour, timezone.utc).isoformat(), "rate": rate}
                    for hour, rate in db.execute(
                        "SELECT hour, rate FROM rate_snapshots WHERE base=? AND target=? AND hour>? AND hour<=? ORDER BY hour",
                        (base, target, now - 24 * HOUR, now),
                    )]

    async def sample(self, client, pins):
        while True:
            now = int(time())
            hour = now // HOUR * HOUR
            try:
                configured = [{"base": self.rates.base, "target": code} for code in self.rates.targets]
                pinned = await asyncio.to_thread(pins.list)
                pairs = [dict(base=base, target=target) for base, target in dict.fromkeys(
                    (item["base"], item["target"]) for item in [*configured, *pinned]
                )]
                quotes = await self.rates.pinned(client, pairs)
                await asyncio.to_thread(self.store, hour, quotes)
            except Exception as error:
                logger.info("记录汇率快照失败（%s）", type(error).__name__)
            await asyncio.sleep(max(1, hour + HOUR - time()))

    async def daily(self, client, base, target, window):
        end = datetime.now(timezone.utc).date()
        count = HISTORY_DAYS[window]
        start = end - timedelta(days=count - 1)
        if base == target:
            return [{"time": (start + timedelta(days=i)).isoformat(), "rate": 1} for i in range(count)], "live"

        key = (base, target, window)
        async with self.locks[base]:
            expires, cached_day, previous, status = self.cache.get(key, (0, None, [], "unavailable"))
            if cached_day == end and monotonic() < expires:
                self.cache.move_to_end(key)
                return previous, status
            try:
                reply = await client.get("https://api.frankfurter.dev/v2/rates", params={
                    "base": base, "quotes": target, "from": start.isoformat(), "to": end.isoformat(),
                }, timeout=8)
                reply.raise_for_status()
                rows = {}
                for item in reply.json():
                    day = datetime.fromisoformat(item["date"]).date()
                    value = float(item["rate"])
                    if item["base"] == base and item["quote"] == target and start <= day <= end and value > 0 and math.isfinite(value):
                        rows[day.isoformat()] = value
                points = [{"time": day, "rate": value} for day, value in sorted(rows.items())]
            except (httpx.HTTPError, KeyError, ValueError, TypeError, OverflowError):
                points = []
            if points:
                status = "live"
            else:
                points = [point for point in previous if start.isoformat() <= point["time"] <= end.isoformat()]
                status = "stale" if points else "unavailable"
            self.cache[key] = (monotonic() + (HOUR if status == "live" else 60), end, points, status)
            self.cache.move_to_end(key)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            return points, status

    async def read(self, client, base, target, window):
        self.rates.validate(base, target)
        if window != "24h":
            points, status = await self.daily(client, base, target, window)
            granularity = "daily"
        else:
            points = await asyncio.to_thread(self.snapshots, base, target, int(time()))
            granularity, status = "hourly_snapshot", "live"
            if not points:
                quote = (await self.rates.quotes(client, base, [target]))[target]
                points = [{"time": quote["date"], "rate": quote["rate"]}] if quote["date"] and quote["rate"] is not None else []
                granularity, status = "daily", quote["status"] if points else "unavailable"
                if base == target:
                    points = [{"time": datetime.now(timezone.utc).date().isoformat(), "rate": 1}]
                    status = "live"
        return {"base": base, "target": target, "range": window, "granularity": granularity, "status": status, "points": points}
