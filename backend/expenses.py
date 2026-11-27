"""账单、Apple Pay 交易和订阅续费提醒。"""

import asyncio
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from .config import WebhookConfig
from .monitor import Rules, send_webhook, utc_now
from .rates import Rates
from .scratchpad import ItemID


logger = logging.getLogger(__name__)
WALLET_AMOUNT = re.compile(
    r"(?:(?:[A-Z]{2})?[$€£¥￥₹₩]\s*|[A-Z]{3}\s+)?"
    r"(?P<number>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)"
    r"(?:\s+[A-Z]{3})?"
)
ZERO_DECIMALS = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF",
                 "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"}
THREE_DECIMALS = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
FOUR_DECIMALS = {"CLF", "UYW"}
RECEIPT_COLUMNS = "id, amount_minor, currency, merchant, category, card, note, source, occurred_at"
NAME = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SHORT_TEXT = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
NOTE = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
CURRENCY = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")]
INGEST_KEY = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CYCLE = Literal["month", "year"]


def parse_amount(value):
    if isinstance(value, str):
        match = WALLET_AMOUNT.fullmatch(value.strip().upper()) if len(value) <= 64 else None
        if not match:
            raise ValueError("金额格式无效")
        return match["number"].replace(",", "")
    return value


AMOUNT = Annotated[Decimal, BeforeValidator(parse_amount), Field(gt=0, le=999_999_999_999)]


class PaymentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: AMOUNT
    currency: CURRENCY | None = None


class ReceiptInput(PaymentInput):
    merchant: NAME
    category: SHORT_TEXT = ""
    card: SHORT_TEXT = ""
    note: NOTE = ""
    occurred_at: AwareDatetime | None = None


class ApplePayInput(ReceiptInput):
    transaction_id: INGEST_KEY | None = None


class SubscriptionInput(PaymentInput):
    name: NAME
    cycle: CYCLE
    next_billing_date: date
    active: bool = True


class PaymentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: AMOUNT | None = None
    currency: CURRENCY | None = None


class ReceiptPatch(PaymentPatch):
    merchant: NAME | None = None
    category: SHORT_TEXT | None = None
    card: SHORT_TEXT | None = None
    note: NOTE | None = None
    occurred_at: AwareDatetime | None = None


class SubscriptionPatch(PaymentPatch):
    name: NAME | None = None
    cycle: CYCLE | None = None
    next_billing_date: date | None = None
    active: bool | None = None


def precision(currency):
    if currency in ZERO_DECIMALS:
        return 0
    if currency in THREE_DECIMALS:
        return 3
    if currency in FOUR_DECIMALS:
        return 4
    return 2


def amount_from_minor(amount_minor, currency):
    return Decimal(amount_minor).scaleb(-precision(currency))


def amount_text(amount_minor, currency):
    return format(amount_from_minor(amount_minor, currency), "f")


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def next_billing_date(anchor: date, cycle: CYCLE, today: date) -> date | None:
    if anchor >= today:
        return anchor
    start = anchor.year * 12 + anchor.month - 1
    current = today.year * 12 + today.month - 1
    interval = 12 if cycle == "year" else 1
    month_index = start + (current - start) // interval * interval
    for index in (month_index, month_index + interval):
        year, month = divmod(index, 12)
        if year > date.max.year:
            return None
        month += 1
        candidate = date(year, month, min(anchor.day, monthrange(year, month)[1]))
        if candidate >= today:
            return candidate
    return None


def public_payment(row):
    item = dict(row)
    item["amount"] = amount_text(item.pop("amount_minor"), item["currency"])
    return item


def public_subscription(row, today):
    result = public_payment(row)
    due = next_billing_date(date.fromisoformat(result.pop("billing_anchor")), result["cycle"], today)
    result["next_billing_date"] = due.isoformat() if due else None
    result["active"] = bool(result["active"])
    return result


def sort_subscriptions(items):
    return sorted(items, key=lambda item: (not item["active"], item["next_billing_date"] or "9999-12-31", item["id"]))


class ExpenseStore:
    def __init__(self, connect, zone: ZoneInfo, rates: Rates):
        self.connect = connect
        self.zone = zone
        self.rates = rates
        self.bark_lock = asyncio.Lock()

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    category TEXT NOT NULL,
                    card TEXT NOT NULL,
                    note TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('manual', 'apple_pay')),
                    occurred_at TEXT NOT NULL,
                    ingest_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS expenses_date ON expenses(occurred_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    cycle TEXT NOT NULL CHECK(cycle IN ('month', 'year')),
                    billing_anchor TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS subscription_reminders (
                    subscription_id INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    PRIMARY KEY(subscription_id, due_date)
                );
            """)

    def local_date(self, now=None):
        return (now or datetime.now(timezone.utc)).astimezone(self.zone).date()

    def money(self, amount, currency):
        currency = currency or self.rates.base
        if amount is None:
            raise HTTPException(422, "请输入金额")
        digits = precision(currency)
        numerator, denominator = amount.as_integer_ratio()
        minor, remainder = divmod(numerator * 10**digits, denominator)
        if remainder:
            raise HTTPException(422, f"{currency} 金额最多支持 {digits} 位小数")
        return minor, currency

    def month_receipts(self, today):
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        bounds = tuple(stamp(datetime.combine(day, time.min, self.zone)) for day in (start, end))
        with self.connect() as db:
            return db.execute(f"""SELECT {RECEIPT_COLUMNS} FROM expenses
                WHERE occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at DESC, id DESC""", bounds).fetchall()

    def receipt(self, db, item_id):
        row = db.execute(f"SELECT {RECEIPT_COLUMNS} FROM expenses WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这笔账单")
        return public_payment(row)

    def list_receipts(self):
        today = self.local_date()
        return {"date": today.isoformat(), "time_zone": self.zone.key, "base_currency": self.rates.base,
                "currencies": self.rates.currencies, "items": [public_payment(row) for row in self.month_receipts(today)]}

    def get_receipt(self, item_id):
        with self.connect() as db:
            return self.receipt(db, item_id)

    def add_receipt(self, payload, *, source="manual", ingest_key=None):
        amount_minor, currency = self.money(payload.amount, payload.currency)
        occurred_at = stamp(payload.occurred_at or datetime.now(timezone.utc))
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO expenses(amount_minor,currency,merchant,category,card,note,source,occurred_at,ingest_key)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ingest_key) DO NOTHING""",
                                (amount_minor, currency, payload.merchant, payload.category, payload.card, payload.note,
                                 source, occurred_at, ingest_key))
            item_id = cursor.lastrowid if cursor.rowcount else db.execute(
                "SELECT id FROM expenses WHERE ingest_key=?", (ingest_key,)).fetchone()["id"]
            return self.receipt(db, item_id)

    def update_receipt(self, item_id, update):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fields = self.receipt(db, item_id)
            fields["amount"] = Decimal(fields["amount"])
            fields.update(update.model_dump(exclude_unset=True))
            if any(fields[key] is None for key in ("merchant", "category", "card", "note")):
                raise HTTPException(422, "请填写有效的账单资料")
            amount_minor, currency = self.money(fields["amount"], fields["currency"])
            occurred_at = fields["occurred_at"]
            if not isinstance(occurred_at, str):
                occurred_at = stamp(occurred_at or datetime.now(timezone.utc))
            db.execute("""UPDATE expenses SET amount_minor=?,currency=?,merchant=?,category=?,card=?,note=?,occurred_at=?
                WHERE id=?""", (amount_minor, currency, fields["merchant"], fields["category"],
                                fields["card"], fields["note"], occurred_at, item_id))
            return self.receipt(db, item_id)

    def delete_receipt(self, item_id):
        with self.connect() as db:
            if not db.execute("DELETE FROM expenses WHERE id=?", (item_id,)).rowcount:
                raise HTTPException(404, "找不到这笔账单")

    def subscriptions(self, today):
        with self.connect() as db:
            rows = db.execute("""SELECT subscriptions.*, MAX(subscription_reminders.due_date) AS reminded_date
                FROM subscriptions LEFT JOIN subscription_reminders ON subscriptions.id=subscription_reminders.subscription_id
                GROUP BY subscriptions.id""")
            return sort_subscriptions(public_subscription(row, today) for row in rows)

    def subscription_row(self, db, item_id):
        row = db.execute("""SELECT *, (SELECT MAX(due_date) FROM subscription_reminders WHERE subscription_id=?) AS reminded_date
                FROM subscriptions WHERE id=?""", (item_id, item_id)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这项订阅")
        return row

    def get_subscription(self, item_id):
        with self.connect() as db:
            return public_subscription(self.subscription_row(db, item_id), self.local_date())

    def add_subscription(self, payload):
        amount_minor, currency = self.money(payload.amount, payload.currency)
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO subscriptions(name,amount_minor,currency,cycle,billing_anchor,active)
                VALUES (?,?,?,?,?,?)""", (payload.name, amount_minor, currency, payload.cycle,
                                          payload.next_billing_date.isoformat(), payload.active))
            return public_subscription(self.subscription_row(db, cursor.lastrowid), self.local_date())

    def update_subscription(self, item_id, update):
        today = self.local_date()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.subscription_row(db, item_id)
            current = public_subscription(row, today)
            fields = {**current, "amount": Decimal(current["amount"]), **update.model_dump(exclude_unset=True)}
            if any(fields[key] is None for key in ("name", "cycle", "active")) or (
                "next_billing_date" in update.model_fields_set and update.next_billing_date is None
            ):
                raise HTTPException(422, "请填写有效的订阅资料")
            amount_minor, currency = self.money(fields["amount"], fields["currency"])
            due = fields["next_billing_date"]
            if isinstance(due, date):
                due = due.isoformat()
            anchor = row["billing_anchor"]
            if due is not None and (due != current["next_billing_date"] or fields["cycle"] != current["cycle"]):
                anchor = due
            db.execute("""UPDATE subscriptions SET name=?,amount_minor=?,currency=?,cycle=?,billing_anchor=?,active=?
                WHERE id=?""", (fields["name"], amount_minor, currency, fields["cycle"], anchor, fields["active"], item_id))
            return public_subscription(self.subscription_row(db, item_id), today)

    def delete_subscription(self, item_id):
        with self.connect() as db:
            if not db.execute("DELETE FROM subscriptions WHERE id=?", (item_id,)).rowcount:
                raise HTTPException(404, "找不到这项订阅")
            db.execute("DELETE FROM subscription_reminders WHERE subscription_id=?", (item_id,))

    def snapshot(self, today):
        receipts = self.month_receipts(today)
        totals = defaultdict(int)
        for row in receipts:
            totals[row["currency"]] += row["amount_minor"]
        upcoming = [item for item in self.subscriptions(today) if item["active"] and item["next_billing_date"]]
        return receipts, totals, upcoming

    async def summary(self, client):
        today = self.local_date()
        receipts, totals, upcoming = await run_in_threadpool(self.snapshot, today)
        base = self.rates.base
        targets = sorted(set(totals).intersection(self.rates.currencies) - {base})
        quotes = await self.rates.quotes(client, base, targets) if targets else {}
        total = amount_from_minor(totals.get(base, 0), base)
        missing = []
        for currency in sorted(set(totals) - {base}):
            rate = quotes.get(currency, {}).get("rate")
            if rate is None:
                missing.append(currency)
            else:
                total += amount_from_minor(totals[currency], currency) / Decimal(str(rate))
        converted = total.quantize(Decimal(1).scaleb(-precision(base)), rounding=ROUND_HALF_UP)
        return {"date": today.isoformat(), "time_zone": self.zone.key, "base_currency": base,
                "month_total": format(converted, "f") if not totals or len(totals) > len(missing) else None,
                "monthly_count": len(receipts), "missing_rates": missing,
                "totals_by_currency": {code: amount_text(minor, code) for code, minor in sorted(totals.items())},
                "recent_expenses": [public_payment(row) for row in receipts[:4]], "upcoming_bills": upcoming}

    def due_reminders(self, today):
        deadline = (today + timedelta(days=3)).isoformat()
        items = self.subscriptions(today)
        with self.connect() as db:
            reminded = {tuple(row) for row in db.execute("""SELECT subscription_id,due_date FROM subscription_reminders
                WHERE due_date BETWEEN ? AND ?""", (today.isoformat(), deadline))}
        return [item for item in items if item["active"] and item["next_billing_date"]
                and item["next_billing_date"] <= deadline and (item["id"], item["next_billing_date"]) not in reminded]

    def record_reminder(self, item_id, due):
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO subscription_reminders(subscription_id,due_date)
                SELECT id, ? FROM subscriptions WHERE id=?""", (due, item_id))

    async def bark_if_due(self, client, webhook: WebhookConfig, now=None):
        if webhook.kind != "bark":
            return
        async with self.bark_lock:
            today = self.local_date(now)
            for item in await run_in_threadpool(self.due_reminders, today):
                due = item["next_billing_date"]
                message = f"{item['name']} 将于 {due} 续费，金额 {item['amount']} {item['currency']}。"
                event = Rules.event("subscription_due", "订阅续费提醒", message, utc_now())
                try:
                    await asyncio.wait_for(send_webhook(client, webhook, event), webhook.timeout_seconds)
                except Exception as error:
                    logger.warning("订阅续费提醒发送失败（%s）", type(error).__name__)
                else:
                    await run_in_threadpool(self.record_reminder, item["id"], due)

    async def run_bark(self, client, webhook: WebhookConfig):
        while True:
            try:
                await self.bark_if_due(client, webhook)
            except Exception as error:
                logger.warning("无法检查订阅续费提醒（%s）", type(error).__name__)
            await asyncio.sleep(300)


def expenses_router(store: ExpenseStore):
    router = APIRouter(prefix="/api", tags=["expenses"])

    @router.get("/expenses")
    def list_receipts(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.list_receipts()

    @router.get("/expenses/summary")
    async def summary(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await store.summary(request.app.state.external_client)

    @router.post("/expenses", status_code=201)
    def create_receipt(payload: ReceiptInput):
        return store.add_receipt(payload)

    @router.post("/expenses/ingest", status_code=201)
    def ingest(payload: ApplePayInput, idempotency_key: Annotated[INGEST_KEY | None, Header()] = None):
        if idempotency_key and payload.transaction_id and idempotency_key != payload.transaction_id:
            raise HTTPException(422, "交易 ID 与幂等键不一致")
        return store.add_receipt(payload, source="apple_pay", ingest_key=idempotency_key or payload.transaction_id)

    @router.get("/expenses/{item_id}")
    def get_receipt(item_id: ItemID, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.get_receipt(item_id)

    @router.put("/expenses/{item_id}")
    @router.patch("/expenses/{item_id}")
    def update_receipt(item_id: ItemID, update: ReceiptPatch):
        return store.update_receipt(item_id, update)

    @router.delete("/expenses/{item_id}", status_code=204)
    def delete_receipt(item_id: ItemID):
        store.delete_receipt(item_id)

    @router.get("/subscriptions")
    def list_subscriptions(response: Response):
        response.headers["Cache-Control"] = "no-store"
        today = store.local_date()
        return {"date": today.isoformat(), "base_currency": store.rates.base,
                "currencies": store.rates.currencies, "items": store.subscriptions(today)}

    @router.post("/subscriptions", status_code=201)
    def create_subscription(payload: SubscriptionInput):
        return store.add_subscription(payload)

    @router.get("/subscriptions/{item_id}")
    def get_subscription(item_id: ItemID, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.get_subscription(item_id)

    @router.put("/subscriptions/{item_id}")
    @router.patch("/subscriptions/{item_id}")
    def update_subscription(item_id: ItemID, update: SubscriptionPatch):
        return store.update_subscription(item_id, update)

    @router.delete("/subscriptions/{item_id}", status_code=204)
    def delete_subscription(item_id: ItemID):
        store.delete_subscription(item_id)

    return router
