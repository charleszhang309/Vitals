import asyncio
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from backend.ai import complete_text
from backend.ai_log_usage import LogUsage
from backend.ai_usage import AiUsage, UsageLedger, usage_router
from backend.config import AiUsageConfig, Settings
from backend.main import create_app
from backend.scratchpad import Scratchpad
from backend.tests.test_stage10 import app_for, client_for


def codex_quota_record(timestamp, primary, secondary, info=None):
    return {"timestamp": timestamp, "type": "event_msg", "payload": {
        "type": "token_count", "info": info, "rate_limits": {"primary": primary, "secondary": secondary},
    }}


@pytest.mark.parametrize("primary,secondary,expected", [
    pytest.param(
        {"used_percent": 23.7, "window_minutes": 10080, "resets_at": 1767952800}, None,
        {"five_hour": None, "seven_day": {"remaining_percent": 76, "resets_at": 1767952800}}, id="pro",
    ),
    pytest.param(
        {"used_percent": 104, "window_minutes": 300, "resets_at": 1767445200},
        {"used_percent": -1, "window_minutes": 10080, "resets_at": 1767952800},
        {"five_hour": {"remaining_percent": 0, "resets_at": 1767445200},
         "seven_day": {"remaining_percent": 100, "resets_at": 1767952800}}, id="plus",
    ),
])
def test_codex_usage_returns_the_latest_quota_snapshot(tmp_path, primary, secondary, expected):
    root = tmp_path / "sessions"
    root.mkdir()
    old = codex_quota_record("2026-01-03T09:00:00Z", {"used_percent": 14, "window_minutes": 300}, None,
                             {"total_token_usage": {"total_tokens": 42}})
    valid = codex_quota_record("2026-01-03T10:00:00Z", primary, secondary)
    invalid = codex_quota_record("2026-01-03T11:00:00Z", {"used_percent": True, "window_minutes": 300},
                                 {"used_percent": 22, "window_minutes": 720})
    (root / "old.jsonl").write_text(f"{json.dumps(old)}\n{json.dumps(invalid)}\n", encoding="utf-8")
    (root / "new.jsonl").write_text(json.dumps(valid), encoding="utf-8")

    client_usage = AiUsage(None, AiUsageConfig(), ZoneInfo("UTC"), local=LogUsage(root))
    now = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)
    result = client_usage.local_source("codex", "Codex", now.date(), now.date(), now)

    assert result["quota"] == expected
    assert result["today_tokens"] == 42


def test_vitals_counts_stream_usage_once_and_records_briefing(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.ai_usage.LogUsage", lambda: LogUsage(tmp_path / "no-codex", tmp_path / "no-claude"))
    def upstream(request):
        payload = json.loads(request.content)
        if payload["stream"]:
            assert payload["stream_options"] == {"include_usage": True}
            chunks = [
                {"choices": [{"delta": {"content": "测试回复。"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"total_tokens": 123}},
                {"choices": [], "usage": {"total_tokens": 123}},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(item)}\n\n" for item in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "简报。"}}],
                                        "usage": {"prompt_tokens": 25, "completion_tokens": 7}})

    async def scenario():
        app = app_for(tmp_path, upstream)
        ledger = UsageLedger(Scratchpad(tmp_path).connect)
        ledger.initialize()
        async with app.state.external_client, client_for(app) as client:
            chat = await client.post("/api/ai/chat", json={"message": "测试用量"})
            assert "event: done" in chat.text
            config = Settings(ai={"enabled": True, "api_key": "fixture-key"}).ai
            await complete_text(app.state.external_client, config, "测试", "生成简报", 100, ledger)
            response = await client.get("/api/ai/usage")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        vitals = next(item for item in response.json()["sources"] if item["id"] == "vitals")
        assert vitals["total_tokens"] == vitals["today_tokens"] == vitals["peak_tokens"] == 155
        assert len(vitals["weeks"]) == 12 and all(len(week) == 7 for week in vitals["weeks"])
        assert vitals["weeks"][-1][-1] == {"date": response.json()["today"], "tokens": 155}

    asyncio.run(scenario())


def test_cloud_usage_paginates_caches_and_distinguishes_balance(tmp_path):
    ledger = UsageLedger(Scratchpad(tmp_path).connect)
    ledger.initialize()
    midnight = datetime.combine(datetime.now(timezone.utc).date(), datetime.min.time(), timezone.utc)
    calls = []

    def upstream(request):
        calls.append(request.url.host)
        if request.url.host == "api.deepseek.com":
            assert request.url.path == "/user/balance"
            assert request.headers["authorization"] == "Bearer fixture-deepseek"
            return httpx.Response(200, json={"balance_infos": [{"currency": "USD", "total_balance": "9.25"}]})
        if request.url.host == "api.openai.com":
            assert request.url.path == "/v1/organization/usage/completions"
            assert request.url.params["bucket_width"] == "1d"
            if request.url.params.get("page") is None:
                return httpx.Response(200, json={"data": [], "has_more": True, "next_page": "fixture-page"})
            assert request.url.params["page"] == "fixture-page"
            return httpx.Response(200, json={"data": [{"start_time": int(midnight.timestamp()), "results": [
                {"input_tokens": 250, "input_cached_tokens": 100, "output_tokens": 50},
            ]}]})
        assert request.url.path == "/v1/organizations/usage_report/messages"
        assert request.headers["x-api-key"] == "fixture-anthropic"
        return httpx.Response(200, json={"data": [{"starting_at": midnight.isoformat(), "results": [
            {"uncached_input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 30,
             "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 7}},
        ]}]})

    usage = AiUsage(ledger, AiUsageConfig(deepseek_api_key="fixture-deepseek", openai_admin_key="fixture-openai",
                                         anthropic_admin_key="fixture-anthropic"), ZoneInfo("UTC"),
                    local=LogUsage(tmp_path / "no-codex", tmp_path / "no-claude"))
    app = FastAPI()
    app.include_router(usage_router(usage))
    app.state.external_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = TestClient(app)
    first = client.get("/api/ai/usage")
    assert first.status_code == 200
    assert "fixture-" not in first.text
    result = {item["id"]: item for item in first.json()["sources"]}
    assert not result["codex"]["available"]
    assert result["codex"]["quota"] == {"five_hour": None, "seven_day": None}
    assert result["deepseek"]["available"] and result["deepseek"]["today_tokens"] is None
    assert result["deepseek"]["balances"] == [{"currency": "USD", "amount": "9.25"}]
    assert result["openai"]["today_tokens"] == 300
    assert result["anthropic"]["today_tokens"] == 70
    again = {item["id"]: item for item in client.get("/api/ai/usage").json()["sources"]}
    for provider in ("deepseek", "openai", "anthropic"):
        assert result[provider]["updated_at"]
        assert again[provider] == result[provider]
    assert result["codex"]["updated_at"] is None
    assert result["vitals"]["updated_at"] and again["vitals"]["updated_at"] >= result["vitals"]["updated_at"]
    assert calls.count("api.openai.com") == 2 and len(calls) == 4


def test_setup_keeps_optional_usage_keys_private_and_preserves_blank(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"ai_usage":{"openai_admin_key":"fixture-private-admin"}}', encoding="utf-8")
    app = create_app(config_path=path, data_dir=tmp_path / "data")
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000))
    response = client.patch("/api/setup/config", json={"ai_usage": {
        "openai_admin_key": "", "deepseek_api_key": "fixture-private-balance",
    }})
    assert response.status_code == 200
    assert response.json()["ai_usage"] == {
        "has_openai_admin_key": True, "has_deepseek_api_key": True, "has_anthropic_admin_key": False,
    }
    assert response.json()["restart_required"]
    assert "fixture-private-" not in response.text
    assert json.loads(path.read_text(encoding="utf-8"))["ai_usage"]["openai_admin_key"] == "fixture-private-admin"


def test_cloud_failure_does_not_look_like_zero_usage(tmp_path):
    ledger = UsageLedger(Scratchpad(tmp_path).connect)
    ledger.initialize()
    usage = AiUsage(ledger, AiUsageConfig(openai_admin_key="fixture-key"), ZoneInfo("UTC"),
                    local=LogUsage(tmp_path / "no-codex", tmp_path / "no-claude"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as client:
            result = await usage.summary(client)
        sources = {value["id"]: value for value in result["sources"]}
        assert sources["openai"]["available"] is False
        assert sources["openai"]["today_tokens"] is None
        assert sources["vitals"]["today_tokens"] == 0

    asyncio.run(scenario())
