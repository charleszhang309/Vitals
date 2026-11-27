"""Local token accounting and optional organization usage summaries."""

import asyncio
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
import sqlite3
from time import monotonic

from fastapi import APIRouter, Request, Response
import httpx
from starlette.concurrency import run_in_threadpool

from .ai_log_usage import LogUsage


DAYS = 12 * 7
OPENAI_USAGE = "https://api.openai.com/v1/organization/usage/completions"
ANTHROPIC_USAGE = "https://api.anthropic.com/v1/organizations/usage_report/messages"
DEEPSEEK_BALANCE = "https://api.deepseek.com/user/balance"
logger = logging.getLogger(__name__)


def token_count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else 0


class UsageLedger:
    def __init__(self, connect):
        self.connect = connect

    def initialize(self):
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_token_usage (
                recorded_at INTEGER NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ai_token_usage_time ON ai_token_usage(recorded_at)")

    def insert(self, stamp, kind, tokens):
        with self.connect() as db:
            db.execute("INSERT INTO ai_token_usage VALUES (?, ?, ?)", (stamp, kind, tokens))

    async def record(self, data, kind):
        if not isinstance(data, dict):
            return False
        count = token_count(data.get("total_tokens")) or (
            token_count(data.get("prompt_tokens")) + token_count(data.get("completion_tokens"))
        )
        if not count:
            return False
        try:
            await run_in_threadpool(self.insert, int(datetime.now(timezone.utc).timestamp()), kind, count)
        except sqlite3.Error as error:
            logger.warning("记录 Vitals AI 用量失败（%s）", type(error).__name__)
        return True

    def daily(self, start, end, zone):
        low = int(datetime.combine(start, time.min, zone).timestamp())
        high = int(datetime.combine(end + timedelta(days=1), time.min, zone).timestamp())
        totals = Counter()
        with self.connect() as db:
            for stamp, count in db.execute(
                "SELECT recorded_at, tokens FROM ai_token_usage WHERE recorded_at >= ? AND recorded_at < ?", (low, high)
            ):
                totals[datetime.fromtimestamp(stamp, zone).date()] += count
        return totals


def week_summary(source, name, start, end, zone, totals=None, balances=None, updated_at=None):
    available = totals is not None or balances is not None
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    cells = [{"date": day.isoformat(), "tokens": totals.get(day, 0)} for day in days] if totals is not None else []
    values = [cell["tokens"] for cell in cells]
    result = {
        "id": source, "name": name, "available": available, "timezone": str(zone),
        "today_tokens": values[-1] if values else None, "total_tokens": sum(values) if values else None,
        "peak_tokens": max(values) if values else None,
        "weeks": [cells[index:index + 7] for index in range(0, len(cells), 7)],
        "updated_at": updated_at.isoformat(timespec="seconds") if available and updated_at else None,
    }
    if balances is not None:
        result["balances"] = balances
    return result


async def provider_json(client, url, headers, params=None):
    response = await client.get(url, headers=headers, params=params, timeout=6)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid usage response")
    return payload


def bucket_total(row, provider):
    if provider == "openai":
        # OpenAI's input_tokens already includes cached input.
        return token_count(row.get("input_tokens")) + token_count(row.get("output_tokens"))
    cache = row.get("cache_creation")
    created = token_count(row.get("cache_creation_input_tokens"))
    if not created and isinstance(cache, dict):
        created = sum(token_count(cache.get(key)) for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    return (created + token_count(row.get("uncached_input_tokens"))
            + token_count(row.get("cache_read_input_tokens")) + token_count(row.get("output_tokens")))


async def daily_provider_usage(client, provider, key, start, now):
    midnight = datetime.combine(start, time.min, timezone.utc)
    if provider == "openai":
        url, headers = OPENAI_USAGE, {"Authorization": f"Bearer {key}"}
        bounds = {"start_time": int(midnight.timestamp()), "end_time": int(now.timestamp())}
    else:
        url, headers = ANTHROPIC_USAGE, {"x-api-key": key, "anthropic-version": "2026-10-21"}
        bounds = {"starting_at": midnight.isoformat(), "ending_at": now.isoformat()}
    params = {**bounds, "bucket_width": "1d", "limit": 31}
    totals = Counter()
    cursors = set()
    for _ in range(8):
        payload = await provider_json(client, url, headers, params)
        for bucket in payload["data"]:
            if provider == "openai":
                day = datetime.fromtimestamp(bucket["start_time"], timezone.utc).date()
            else:
                day = datetime.fromisoformat(bucket["starting_at"]).astimezone(timezone.utc).date()
            if start <= day <= now.date():
                totals[day] += sum(bucket_total(row, provider) for row in bucket["results"])
        if not payload.get("has_more"):
            return totals
        cursor = payload.get("next_page")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            break
        cursors.add(cursor)
        params["page"] = cursor
    raise ValueError("incomplete usage response")


async def deepseek_balances(client, key):
    payload = await provider_json(client, DEEPSEEK_BALANCE, {"Authorization": f"Bearer {key}"})
    balances = []
    for balance in payload["balance_infos"]:
        currency, amount = balance.get("currency"), balance.get("total_balance")
        if isinstance(currency, str) and isinstance(amount, str) and len(currency) <= 8 and len(amount) <= 32:
            try:
                if Decimal(amount).is_finite():
                    balances.append({"currency": currency, "amount": amount})
            except InvalidOperation:
                continue
    return balances


class AiUsage:
    def __init__(self, ledger, settings, zone, local=None):
        self.ledger = ledger
        self.settings = settings
        self.zone = zone
        self.local = local if local is not None else LogUsage()
        self.cloud = {}
        self.locks = {provider: asyncio.Lock() for provider in ("deepseek", "openai", "anthropic")}

    def local_source(self, source, name, start, end, now):
        try:
            if source == "vitals":
                totals = self.ledger.daily(start, end, self.zone)
            else:
                totals = self.local.daily(source, start, end, self.zone) if self.local.available(source) else None
        except (OSError, sqlite3.Error) as error:
            logger.info("读取 AI 用量失败（%s，%s）", source, type(error).__name__)
            totals = None
        return week_summary(source, name, start, end, self.zone, totals=totals, updated_at=now)

    async def provider_source(self, provider, name, setting, client, now):
        end = now.date()
        start = end - timedelta(days=DAYS - 1)
        secret = getattr(self.settings, setting)
        key = secret.get_secret_value().strip() if secret else ""
        if not key:
            return week_summary(provider, name, start, end, timezone.utc)
        async with self.locks[provider]:
            day, expires, cached = self.cloud.get(provider, (None, 0, None))
            if day == end and monotonic() < expires:
                return cached
            try:
                if provider == "deepseek":
                    balances = await asyncio.wait_for(deepseek_balances(client, key), 8)
                    result = week_summary(provider, name, start, end, timezone.utc, balances=balances, updated_at=now)
                else:
                    totals = await asyncio.wait_for(daily_provider_usage(client, provider, key, start, now), 24)
                    result = week_summary(provider, name, start, end, timezone.utc, totals=totals, updated_at=now)
                ttl = 600
            except (httpx.HTTPError, TimeoutError, KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                logger.info("读取云端 AI 用量失败（%s，%s）", provider, type(error).__name__)
                result = ({**cached, "stale": True} if day == end and cached and cached["available"]
                          else week_summary(provider, name, start, end, timezone.utc))
                ttl = 60
            self.cloud[provider] = (end, monotonic() + ttl, result)
            return result

    async def summary(self, client, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        today = now.astimezone(self.zone).date()
        start = today - timedelta(days=DAYS - 1)
        sources = await asyncio.gather(
            *(run_in_threadpool(self.local_source, source, name, start, today, now)
              for source, name in (("codex", "Codex"), ("claude", "Claude Code"), ("vitals", "Vitals"))),
            *(self.provider_source(provider, name, key, client, now) for provider, name, key in (
                ("deepseek", "DeepSeek", "deepseek_api_key"), ("openai", "OpenAI", "openai_admin_key"),
                ("anthropic", "Anthropic", "anthropic_admin_key"),
            )),
        )
        return {"start_date": start.isoformat(), "end_date": today.isoformat(),
                "today": today.isoformat(), "sources": sources}


def usage_router(usage):
    router = APIRouter(prefix="/api/ai", tags=["ai"])

    @router.get("/usage")
    async def get_usage(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await usage.summary(request.app.state.external_client)

    return router
