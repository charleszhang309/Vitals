import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import FinanceConfig, WebhookConfig
from backend.expenses import ExpenseStore, ReceiptInput, SubscriptionInput, expenses_router
from backend.rates import Rates
from backend.scratchpad import Scratchpad


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    scratchpad = Scratchpad(tmp_path)
    scratchpad.initialize()
    rates = Rates(FinanceConfig())
    store = ExpenseStore(scratchpad.connect, ZoneInfo("America/New_York"), rates)
    store.initialize()
    monkeypatch.setattr(store, "local_date", lambda now=None: date(2026, 2, 26) if now is None else now.astimezone(store.zone).date())
    return store


def test_apple_pay_ingest_and_monthly_receipt_crud(ledger):
    app = FastAPI()
    app.include_router(expenses_router(ledger))
    client = TestClient(app)
    payment = {"merchant": "Cafe", "card": "Visa", "amount": "$12.50", "occurred_at": "2026-03-01T04:59:59Z"}
    key = {"Idempotency-Key": "wallet-123"}
    created = client.post("/api/expenses/ingest", json=payment, headers=key)
    assert created.status_code == 201
    receipt = created.json()
    assert (receipt["amount"], receipt["currency"], receipt["card"], receipt["source"]) == ("12.50", "CAD", "Visa", "apple_pay")
    assert client.post("/api/expenses/ingest", json=payment, headers=key).json()["id"] == receipt["id"]
    for amount in ("1.201", "1.00000000000000000000000000000000001"):
        invalid = client.post("/api/expenses", json={"merchant": "Cafe", "amount": amount})
        assert invalid.status_code == 422
    client.post("/api/expenses", json={"merchant": "Next month", "amount": 8, "occurred_at": "2026-03-01T05:00:00Z"})
    assert client.get("/api/expenses").json()["items"] == [receipt]
    path = f"/api/expenses/{receipt['id']}"
    updated = client.patch(path, json={"category": "餐饮", "amount": "15.00"})
    assert updated.json()["merchant"] == "Cafe"
    assert updated.json()["category"] == "餐饮"
    assert updated.json()["amount"] == "15.00"
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_summary_converts_known_rates_and_keeps_unavailable_amounts(ledger):
    app = FastAPI()
    app.include_router(expenses_router(ledger))

    def exchange_rates(request):
        assert request.url.params["base"] == "CAD"
        return httpx.Response(200, json=[{"base": "CAD", "quote": "USD", "rate": 0.8, "date": "2026-02-25"}])

    app.state.external_client = httpx.AsyncClient(transport=httpx.MockTransport(exchange_rates))
    client = TestClient(app)
    assert client.put("/api/expenses/budget", json={"amount": "10.30"}).status_code == 200
    for amount, currency in [("0.10", "CAD"), ("0.20", "CAD"), ("8", "USD"), ("100", "JPY")]:
        receipt = client.post("/api/expenses", json={"merchant": currency, "amount": amount, "currency": currency,
                                                   "occurred_at": "2026-02-25T17:00:00Z"})
        assert receipt.status_code == 201
    reply = client.get("/api/expenses/summary").json()
    assert reply["base_currency"] == "CAD"
    assert reply["month_total"] == "10.30"
    assert reply["monthly_count"] == 4
    assert reply["totals_by_currency"] == {"CAD": "0.30", "JPY": "100", "USD": "8.00"}
    assert reply["missing_rates"] == ["JPY"]
    assert reply["monthly_budget"] == {"amount": "10.30", "remaining": "0.00", "used_percent": 100}


def test_summary_reports_unavailable_rates_when_the_request_stalls(ledger, monkeypatch):
    monkeypatch.setattr("backend.rates.QUOTE_TIMEOUT", 0.01)
    ledger.set_budget(Decimal("100"))
    ledger.add_receipt(ReceiptInput(merchant="Cafe", amount="8.00", currency="USD",
                                    occurred_at="2026-02-25T17:00:00Z"))

    async def stalled_rate(request):
        await asyncio.Event().wait()

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(stalled_rate)) as client:
            reply = await ledger.summary(client)
        assert reply["month_total"] is None
        assert reply["totals_by_currency"] == {"USD": "8.00"}
        assert reply["missing_rates"] == ["USD"]
        assert reply["monthly_budget"] == {"amount": "100.00", "remaining": None, "used_percent": None}

    asyncio.run(verify())


def test_default_currency_persists_and_converts_custom_codes(ledger):
    app = FastAPI()
    app.include_router(expenses_router(ledger))
    catalog_requests = []

    def exchange_rates(request):
        if request.url.path.endswith("/currencies"):
            catalog_requests.append(True)
            return httpx.Response(200, json=[{"iso_code": "AED"}, {"iso_code": "VND"}])
        assert request.url.params["base"] == "VND"
        assert request.url.params["quotes"] == "AED,CAD"
        return httpx.Response(200, json=[
            {"base": "VND", "quote": quote, "rate": rate, "date": "2026-02-25"}
            for quote, rate in (("AED", 0.5), ("CAD", 0.25))
        ])

    app.state.external_client = httpx.AsyncClient(transport=httpx.MockTransport(exchange_rates))
    client = TestClient(app)
    currency_path = "/api/expenses/base-currency"
    budget_path = "/api/expenses/budget"
    assert client.get("/api/expenses").json()["base_currency"] == "CAD"
    assert client.put(budget_path, json={"amount": "100"}).status_code == 200
    assert client.put(currency_path, json={"base_currency": "usd$"}).status_code == 422
    assert [client.put(currency_path, json={"base_currency": code}).json() for code in ("usd", " vnd ")] == [
        {"base_currency": "USD"}, {"base_currency": "VND"},
    ]
    assert ledger.rates.base == "CAD"
    assert ExpenseStore(ledger.connect, ledger.zone, ledger.rates).base_currency() == "VND"
    assert client.get("/api/subscriptions").json()["base_currency"] == "VND"
    assert client.get(budget_path).json() == {"amount": None, "currency": "VND"}
    assert client.put(budget_path, json={"amount": "50"}).json() == {"amount": "50", "currency": "VND"}

    receipt = {"merchant": "Cafe", "amount": "20", "occurred_at": "2026-02-25T17:00:00Z"}
    assert client.post("/api/expenses/ingest", json=receipt).json()["currency"] == "VND"
    for currency, amount in [("CAD", "5"), ("AED", "2"), ("ABC", "2")]:
        assert client.post("/api/expenses", json={**receipt, "currency": currency, "amount": amount}).status_code == 201
    for _ in range(2):
        summary = client.get("/api/expenses/summary").json()
        assert summary["base_currency"] == "VND"
        assert summary["month_total"] == "44"
        assert summary["missing_rates"] == ["ABC"]
        assert summary["monthly_budget"] == {"amount": "50", "remaining": "6", "used_percent": 88}
    assert catalog_requests == [True]
    assert client.put(currency_path, json={"base_currency": "CAD"}).status_code == 200
    assert client.get(budget_path).json() == {"amount": "100.00", "currency": "CAD"}


def test_budget_persists_and_tracks_each_month_in_base_currency(ledger, monkeypatch):
    app = FastAPI()
    app.include_router(expenses_router(ledger))
    app.state.external_client = None
    client = TestClient(app)
    path = "/api/expenses/budget"
    summary = "/api/expenses/summary"
    empty = {"amount": None, "currency": "CAD"}

    assert client.get(path).json() == empty
    assert client.get(summary).json()["monthly_budget"] is None
    amount = {"amount": "100.00", "currency": "CAD"}
    saved = client.put(path, json={"amount": "100"})
    assert saved.status_code == 200
    assert saved.json() == amount
    for payload in ({}, {"amount": 0}, {"amount": -1}, {"amount": "12.345"}, {"amount": "10", "currency": "USD"}):
        assert client.put(path, json=payload).status_code == 422
    reopened = ExpenseStore(ledger.connect, ledger.zone, ledger.rates)
    reopened.initialize()
    assert reopened.budget() == amount
    assert ExpenseStore(ledger.connect, ledger.zone, Rates(FinanceConfig(base_currency="USD"))).budget() == {
        "amount": None, "currency": "USD",
    }

    receipt = client.post("/api/expenses", json={"merchant": "Cafe", "amount": "1",
                                                 "occurred_at": "2026-03-01T04:59:59Z"})
    receipt_path = f"/api/expenses/{receipt.json()['id']}"
    for spent, remaining, percent in [("79.99", "20.01", 79.9), ("80", "20.00", 80), ("100", "0.00", 100),
                                      ("115", "-15.00", 115)]:
        assert client.patch(receipt_path, json={"amount": spent}).status_code == 200
        assert client.get(summary).json()["monthly_budget"] == {
            "amount": "100.00", "remaining": remaining, "used_percent": percent,
        }

    monkeypatch.setattr(ledger, "local_date", lambda now=None: date(2026, 3, 1))
    assert client.get(summary).json()["monthly_budget"] == {"amount": "100.00", "remaining": "100.00", "used_percent": 0}
    assert client.put(path, json={"amount": None}).json() == empty
    assert client.get(summary).json()["monthly_budget"] is None


def test_subscription_crud_preserves_the_billing_anchor(ledger, monkeypatch):
    app = FastAPI()
    app.include_router(expenses_router(ledger))
    client = TestClient(app)
    reply = client.post("/api/subscriptions", json={"name": "Service", "amount": "12.99", "currency": "usd",
                                                    "cycle": "month", "next_billing_date": "2026-01-31"})
    assert reply.status_code == 201
    created = reply.json()
    assert created["next_billing_date"] == "2026-02-28"
    assert created["currency"] == "USD"
    path = f"/api/subscriptions/{created['id']}"
    updated = client.patch(path, json={"next_billing_date": created["next_billing_date"], "active": False})
    assert updated.status_code == 200
    assert not updated.json()["active"]
    monkeypatch.setattr(ledger, "local_date", lambda now=None: date(2026, 3, 1))
    assert client.get(path).json()["next_billing_date"] == "2026-03-31"
    assert len(client.get("/api/subscriptions").json()["items"]) == 1
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_subscriptions_recur_and_retry_bark_only_until_success(ledger):
    annual = ledger.add_subscription(SubscriptionInput(name="Annual", amount="1", cycle="year",
                                                        next_billing_date=date(2024, 2, 29)))
    monthly = ledger.add_subscription(SubscriptionInput(name="Monthly", amount="8", cycle="month",
                                                         next_billing_date=date(2026, 1, 31)))
    assert annual["next_billing_date"] == monthly["next_billing_date"] == "2026-02-28"
    ledger.add_subscription(SubscriptionInput(name="Paused", amount="3", cycle="month", active=False,
                                               next_billing_date=date(2026, 2, 27)))
    urls = []

    def bark(request):
        urls.append(request.url.path)
        return httpx.Response(200, json={"code": 500 if len(urls) == 1 else 200})

    async def remind():
        webhook = WebhookConfig(kind="bark", bark_url_template="https://bark.test/device/{title}/{body}")
        async with httpx.AsyncClient(transport=httpx.MockTransport(bark)) as client:
            now = datetime(2026, 2, 26, 12, tzinfo=timezone.utc)
            await ledger.bark_if_due(client, webhook, now)
            assert ledger.get_subscription(annual["id"])["reminded_date"] is None
            assert ledger.get_subscription(monthly["id"])["reminded_date"] == "2026-02-28"
            await asyncio.gather(ledger.bark_if_due(client, webhook, now), ledger.bark_if_due(client, webhook, now))
            assert ledger.get_subscription(annual["id"])["reminded_date"] == "2026-02-28"
            await ledger.bark_if_due(client, webhook, datetime(2026, 3, 28, 12, tzinfo=timezone.utc))

    asyncio.run(remind())
    assert len(urls) == 4
    assert "Annual" in urls[0] and "Monthly" in urls[1] and "Annual" in urls[2] and "Monthly" in urls[3]
    assert "2026-03-31" in urls[3]
