from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from services.db import db
from services.pending import clear_pending, consume_pending, peek_pending, set_pending
from services import weather


def test_pending_consume_is_kind_scoped_and_atomic() -> None:
    user_id = 91001
    clear_pending(user_id)
    set_pending(user_id, "share", {"token": "x"})

    assert consume_pending(user_id, "gift_target") is None
    assert peek_pending(user_id) is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: consume_pending(user_id, "share"), range(16)))

    consumed = [pending for pending in results if pending is not None]
    assert len(consumed) == 1
    assert consumed[0].kind == "share"
    assert peek_pending(user_id) is None


def test_weather_place_changes_invalidate_cache_and_clear_stale_city(monkeypatch) -> None:
    user_id = 93001
    cache_key = weather._weather_cache_key(user_id)
    weather._WEATHER_CACHE[cache_key] = (weather.time.time(), "old forecast")

    ok, _info = weather.set_location(user_id, 52.3676, 4.9041)
    assert ok is True
    assert cache_key not in weather._WEATHER_CACHE

    with db() as conn:
        row = conn.execute(
            "SELECT city, lat, lon FROM user_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
    assert row is not None
    assert row["city"] is None
    assert float(row["lat"]) == 52.3676
    assert float(row["lon"]) == 4.9041

    monkeypatch.setattr(weather, "_geocode_city", lambda _city: (None, None, None))
    ok, info = weather.set_city(user_id, "Definitely Missing City")
    assert ok is False
    assert "не найден" in info.lower()


def test_weather_forecast_uses_coordinate_timezone(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_http(url: str, timeout: float = 1.2):
        del timeout
        captured["url"] = url
        return {
            "current": {
                "temperature_2m": 10,
                "weather_code": 0,
                "wind_speed_10m": 1,
                "time": "2026-07-27T12:00",
            },
            "hourly": {
                "time": [],
                "temperature_2m": [],
                "weather_code": [],
                "wind_speed_10m": [],
                "precipitation_probability": [],
            },
            "daily": {
                "time": [],
                "temperature_2m_max": [],
                "temperature_2m_min": [],
                "weather_code": [],
                "precipitation_probability_max": [],
            },
        }

    monkeypatch.setattr(weather, "_http_get_json", fake_http)
    weather._build_forecast(52.3676, 4.9041)
    assert "timezone=auto" in captured["url"]
