"""获取 Open-Meteo 天气与空气质量，并分别维护可降级的缓存。"""

import asyncio
from bisect import bisect_right
from datetime import datetime
import logging
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
import httpx

from .config import WeatherConfig
from .monitor import utc_now


logger = logging.getLogger(__name__)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FRESH_SECONDS = 20 * 60
RETRY_SECONDS = 60


class Feed:
    def __init__(self):
        self.data: dict | None = None
        self.updated_at: str | None = None
        self.status = "unavailable"
        self.next_call = 0.0
        self.lock = asyncio.Lock()

    async def read(self, fetch):
        if monotonic() >= self.next_call:
            async with self.lock:
                if monotonic() >= self.next_call:
                    try:
                        self.data = await asyncio.wait_for(fetch(), timeout=7)
                        self.updated_at = utc_now()
                        self.status = "live"
                        self.next_call = monotonic() + FRESH_SECONDS
                    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, TypeError, OverflowError) as error:
                        logger.info("天气上游暂不可用（%s）", type(error).__name__)
                        self.status = "stale" if self.data is not None else "unavailable"
                        self.next_call = monotonic() + RETRY_SECONDS
        return {"status": self.status, "updated_at": self.updated_at, "data": self.data}


def normalize_forecast(raw: dict) -> dict:
    current, hours, days = raw["current"], raw["hourly"], raw["daily"]
    tz = raw["timezone"] or "UTC"
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
        tz = "UTC"
    observed = current["time"]
    today = datetime.fromtimestamp(observed, zone).date().isoformat()
    # 以当地预报的整点为准，也适用于半小时时区和夏令时切换。
    times = hours["time"]
    index = bisect_right(times, observed) - 1
    start = times[index] if index >= 0 else next(iter(times), observed)
    end = start + 24 * 3600
    precipitation = hours["precipitation_probability"][index] if index >= 0 else None
    hourly = [{"time": at, "temperature_c": temperature}
              for at, temperature in zip(times, hours["temperature_2m"]) if start <= at < end]
    daily = [
        {"date": datetime.fromtimestamp(at, zone).date().isoformat(), "code": code, "high_c": high, "low_c": low}
        for at, code, high, low in zip(days["time"], days["weather_code"], days["temperature_2m_max"], days["temperature_2m_min"])
    ][:7]
    today_values = next((item for item in daily if item["date"] == today), {})
    return {
        "timezone": tz, "hourly": hourly, "hourly_start": start, "hourly_end": end, "daily": daily,
        "current": {
            "time": observed, "temperature_c": current.get("temperature_2m"), "code": current.get("weather_code"),
            "feels_like_c": current.get("apparent_temperature"), "humidity_percent": current.get("relative_humidity_2m"),
            "wind_kmh": current.get("wind_speed_10m"), "precipitation_percent": precipitation,
            "high_c": today_values.get("high_c"), "low_c": today_values.get("low_c"),
        },
    }


class Weather:
    def __init__(self, config: WeatherConfig):
        self.config = config
        self.forecast = Feed()
        self.air = Feed()

    async def summary(self, client: httpx.AsyncClient):
        params = {"latitude": self.config.latitude, "longitude": self.config.longitude, "timeformat": "unixtime", "timezone": "auto"}

        async def forecast():
            response = await client.get(FORECAST_URL, params={
                **params, "forecast_days": 7,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "hourly": "temperature_2m,precipitation_probability",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            })
            response.raise_for_status()
            return normalize_forecast(response.json())

        async def air():
            response = await client.get(AIR_URL, params={**params, "current": "us_aqi"})
            response.raise_for_status()
            data = response.json()["current"]
            return {"time": data["time"], "us_aqi": data["us_aqi"]}

        weather, quality = await asyncio.gather(self.forecast.read(forecast), self.air.read(air))
        return {"city_name": self.config.city_name, **weather, "air_quality": quality}


def weather_router(weather: Weather) -> APIRouter:
    router = APIRouter(prefix="/api/weather", tags=["weather"])

    @router.get("")
    async def summary(request: Request):
        return await weather.summary(request.app.state.external_client)

    return router
