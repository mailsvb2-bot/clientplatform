from __future__ import annotations
import logging

"""
Погода (Open‑Meteo, без ключа).

Требования UX:
- показываем: утро / сейчас / вечер / завтра / неделя
- город/координаты храним в SQLite (user_settings)
- кешируем ответ, чтобы не долбить API
"""

import json
import sqlite3
import time
import ssl
import asyncio
from threading import RLock
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.db import db as get_db


def _table_missing(e: Exception) -> bool:
    msg = str(e).lower()
    return "no such table" in msg or "does not exist" in msg


def _schema_note() -> None:
    return


def _weather_cache_key(user_id: int) -> str:
    return f"u:{int(user_id)}"


def _invalidate_weather_cache(user_id: int) -> None:
    with _CACHE_LOCK:
        _WEATHER_CACHE.pop(_weather_cache_key(user_id), None)


def _save_place(user_id: int, *, city: str | None, lat: float, lon: float) -> None:
    latitude = float(lat)
    longitude = float(lon)
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise ValueError("weather coordinates are out of range")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings(user_id, city, lat, lon, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                city=excluded.city,
                lat=excluded.lat,
                lon=excluded.lon,
                updated_at=excluded.updated_at
            """,
            (int(user_id), city, latitude, longitude, time.time()),
        )
    _invalidate_weather_cache(user_id)


def set_city(user_id: int, city: str) -> tuple[bool, str]:
    """Resolve and persist a real city; never save an unverified label as success."""

    city = (city or "").strip()
    if not city:
        return False, "Пустое название города."
    try:
        lat, lon, resolved = _geocode_city(city)
    except WeatherServiceUnavailable:
        return False, "Сервис поиска города временно недоступен. Попробуйте ещё раз."
    if lat is None or lon is None or not resolved:
        return False, "Город не найден. Уточните название, например: «Казань» или «Paris»."
    try:
        _save_place(int(user_id), city=str(resolved), lat=float(lat), lon=float(lon))
        return True, str(resolved)
    except ValueError:
        return False, "Сервис вернул некорректные координаты города."
    except sqlite3.Error as exc:
        if _table_missing(exc):
            return False, "База данных ещё не инициализирована. Перезапустите бота."
        logging.getLogger(__name__).exception("Failed to save city to DB")
        return False, "Не удалось сохранить город. Попробуйте ещё раз."


def set_location(user_id: int, lat: float, lon: float) -> tuple[bool, str]:
    """Persist Telegram coordinates and clear a stale textual city label."""

    try:
        _save_place(int(user_id), city=None, lat=float(lat), lon=float(lon))
        return True, "Локация сохранена."
    except ValueError:
        return False, "Некорректные координаты геолокации."
    except sqlite3.Error as exc:
        if _table_missing(exc):
            return False, "База данных ещё не инициализирована. Перезапустите бота."
        logging.getLogger(__name__).exception("Failed to save location to DB")
        return False, "Не удалось сохранить геолокацию. Попробуйте ещё раз."


def _get_user_place(conn: sqlite3.Connection, user_id: int):
    try:
        row = conn.execute(
            "SELECT city, lat, lon FROM user_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    except sqlite3.Error as e:
        if _table_missing(e):
            return None, None, None
        raise
    if not row:
        return None, None, None
    city = row[0]
    lat = row[1]
    lon = row[2]
    return city, lat, lon


@dataclass
class ForecastPack:
    place: str
    now: str
    morning: str
    evening: str
    tomorrow: str
    week: str


_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_GEO_FAIL_CACHE: dict[str, tuple[float, str]] = {}
_GEO_FAIL_TTL_SEC = 10 * 60
_CACHE_LOCK = RLock()


class WeatherServiceUnavailable(RuntimeError):
    """The external weather/geocoding service is temporarily unavailable."""


def _http_get_json(url: str, timeout: float = 1.2) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "metr-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8"))


def _geocode_city(city: str) -> tuple[float | None, float | None, str | None]:
    city_norm = (city or "").strip()
    if not city_norm:
        return None, None, None

    key = city_norm.casefold()
    with _CACHE_LOCK:
        fail = _GEO_FAIL_CACHE.get(key)
    if fail and (time.time() - fail[0] < _GEO_FAIL_TTL_SEC):
        raise WeatherServiceUnavailable(fail[1])

    q = urllib.parse.quote(city_norm)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=ru&format=json"
    try:
        js = _http_get_json(url, timeout=1.2)
        res = (js.get("results") or [])
        if not res:
            return None, None, None

        r0 = res[0]
        lat = float(r0.get("latitude"))
        lon = float(r0.get("longitude"))
        name = r0.get("name")
        country = r0.get("country")
        place = f"{name}, {country}" if name and country else (name or city_norm)
        return lat, lon, place

    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, ssl.SSLError) as exc:
        with _CACHE_LOCK:
            _GEO_FAIL_CACHE[key] = (time.time(), str(exc))
        logging.getLogger(__name__).warning("Geocoding request failed: %s", exc)
        raise WeatherServiceUnavailable(str(exc)) from exc


def _weather_code_ru(code: int) -> str:
    mapping = {
        0: "ясно",
        1: "в основном ясно",
        2: "переменная облачность",
        3: "пасмурно",
        45: "туман",
        48: "изморозь/туман",
        51: "морось",
        53: "морось",
        55: "морось",
        61: "дождь",
        63: "дождь",
        65: "сильный дождь",
        71: "снег",
        73: "снег",
        75: "сильный снег",
        80: "ливень",
        81: "ливень",
        82: "сильный ливень",
        95: "гроза",
        96: "гроза с градом",
        99: "гроза с градом",
    }
    return mapping.get(int(code), "погода переменная")


def _pick_nearest_hour_index(times: list[str], target_dt: datetime) -> int:
    best_i = 0
    best_d = None
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except (ValueError, TypeError):
            logging.getLogger(__name__).debug("Bad hourly time format", exc_info=True)
            continue
        d = abs((dt - target_dt).total_seconds())
        if best_d is None or d < best_d:
            best_d = d
            best_i = i
    return best_i


def _format_temp(t: float | int | None) -> str:
    if t is None:
        return "—"
    t = float(t)
    sign = "+" if t >= 0 else ""
    return f"{sign}{round(t)}°"


def _format_line(temp: float | int | None, desc: str, wind: float | int | None = None, pop: float | int | None = None) -> str:
    parts = [_format_temp(temp), desc]
    if pop is not None:
        parts.append(f"осадки {round(float(pop))}%")
    if wind is not None:
        parts.append(f"ветер {round(float(wind))} м/с")
    return ", ".join(parts)


def _build_forecast(lat: float, lon: float) -> ForecastPack:
    # The API resolves local time from coordinates. A global server timezone
    # would show wrong morning/evening periods for users in other regions.
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code,wind_speed_10m"
        "&hourly=temperature_2m,weather_code,wind_speed_10m,precipitation_probability"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"
        "&timezone=auto"
    )
    js = _http_get_json(url)

    current = js.get("current") or {}
    now_temp = current.get("temperature_2m")
    now_code = current.get("weather_code")
    now_wind = current.get("wind_speed_10m")
    now_desc = _weather_code_ru(int(now_code)) if now_code is not None else "—"

    hourly = js.get("hourly") or {}
    h_times = hourly.get("time") or []
    h_temp = hourly.get("temperature_2m") or []
    h_code = hourly.get("weather_code") or []
    h_wind = hourly.get("wind_speed_10m") or []
    h_pop = hourly.get("precipitation_probability") or []

    current_time = str(current.get("time") or "").strip()
    try:
        now_dt = datetime.fromisoformat(current_time) if current_time else datetime.now()
    except (ValueError, TypeError):
        logging.getLogger(__name__).debug("Bad current weather time format", exc_info=True)
        now_dt = datetime.now()
    morning_dt = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    evening_dt = now_dt.replace(hour=19, minute=0, second=0, microsecond=0)
    idx_m = _pick_nearest_hour_index(h_times, morning_dt) if h_times else 0
    idx_e = _pick_nearest_hour_index(h_times, evening_dt) if h_times else 0

    morning_line = _format_line(
        h_temp[idx_m] if idx_m < len(h_temp) else None,
        _weather_code_ru(h_code[idx_m]) if idx_m < len(h_code) else "—",
        h_wind[idx_m] if idx_m < len(h_wind) else None,
        h_pop[idx_m] if idx_m < len(h_pop) else None,
    )
    evening_line = _format_line(
        h_temp[idx_e] if idx_e < len(h_temp) else None,
        _weather_code_ru(h_code[idx_e]) if idx_e < len(h_code) else "—",
        h_wind[idx_e] if idx_e < len(h_wind) else None,
        h_pop[idx_e] if idx_e < len(h_pop) else None,
    )

    now_line = _format_line(now_temp, now_desc, now_wind, None)

    daily = js.get("daily") or {}
    d_time = daily.get("time") or []
    d_tmax = daily.get("temperature_2m_max") or []
    d_tmin = daily.get("temperature_2m_min") or []
    d_code = daily.get("weather_code") or []
    d_pop = daily.get("precipitation_probability_max") or []

    tomorrow_line = "—"
    week_line = "—"
    if d_time:
        today_date = now_dt.date()
        t_idx = None
        for i, ds in enumerate(d_time):
            ds_text = str(ds or "").strip()
            if not ds_text:
                continue
            try:
                d = datetime.fromisoformat(ds_text).date()
            except (ValueError, TypeError):
                logging.getLogger(__name__).debug("Bad daily date format", exc_info=True)
                continue
            if d > today_date:
                t_idx = i
                break
        if t_idx is None:
            t_idx = 0
        tmax = d_tmax[t_idx] if t_idx < len(d_tmax) else None
        tmin = d_tmin[t_idx] if t_idx < len(d_tmin) else None
        code = d_code[t_idx] if t_idx < len(d_code) else None
        pop = d_pop[t_idx] if t_idx < len(d_pop) else None
        desc = _weather_code_ru(int(code)) if code is not None else "—"
        tomorrow_line = f"{_format_temp(tmin)}…{_format_temp(tmax)}, {desc}" + (f", осадки {round(float(pop))}%" if pop is not None else "")

        temps = [float(x) for x in d_tmax[:7] if x is not None] + [float(x) for x in d_tmin[:7] if x is not None]
        if temps:
            mn = min(temps)
            mx = max(temps)
            codes = [int(x) for x in d_code[:7] if x is not None]
            dom_desc = _weather_code_ru(max(set(codes), key=codes.count)) if codes else "погода переменная"
            week_line = f"{_format_temp(mn)}…{_format_temp(mx)}, {dom_desc}"
    return ForecastPack(
        place="",
        now=now_line,
        morning=morning_line,
        evening=evening_line,
        tomorrow=tomorrow_line,
        week=week_line,
    )


def get_weather_text(user_id: int) -> str:
    cache_key = _weather_cache_key(user_id)
    with _CACHE_LOCK:
        ts_text = _WEATHER_CACHE.get(cache_key)
    if ts_text and (time.time() - ts_text[0] < 45 * 60):
        return ts_text[1]

    with get_db() as conn:
        city, lat, lon = _get_user_place(conn, int(user_id))

    place = None
    if lat is None or lon is None:
        if city:
            try:
                glat, glon, resolved = _geocode_city(city)
            except WeatherServiceUnavailable:
                return "Погода: сервис определения города временно недоступен. Попробуйте позже."
            if glat is None or glon is None:
                return "Погода: не удалось найти город. Пожалуйста, укажите город иначе (например: «Казань»)."
            lat, lon = glat, glon
            place = resolved or city
            try:
                _save_place(int(user_id), city=str(place), lat=float(lat), lon=float(lon))
            except sqlite3.Error:
                logging.getLogger(__name__).exception("Failed to persist resolved weather place")
        else:
            return "Погода: отправьте геолокацию или напишите город (например: «Москва»)."

    try:
        fc = _build_forecast(float(lat), float(lon))
        place = place or (city or "Ваше местоположение")
        text = (
            f"Погода — {place}\n\n"
            f"🌅 Утро:   {fc.morning}\n"
            f"🌤 Сейчас: {fc.now}\n"
            f"🌆 Вечер:  {fc.evening}\n"
            f"📅 Завтра: {fc.tomorrow}\n"
            f"📆 Неделя: {fc.week}\n"
        )
        with _CACHE_LOCK:
            _WEATHER_CACHE[cache_key] = (time.time(), text)
            if len(_WEATHER_CACHE) > 300:
                overflow = len(_WEATHER_CACHE) - 300
                for key, _value in sorted(_WEATHER_CACHE.items(), key=lambda item: item[1][0])[:overflow]:
                    _WEATHER_CACHE.pop(key, None)
        return text
    except (urllib.error.URLError, TimeoutError, WeatherServiceUnavailable):
        logging.getLogger(__name__).exception("Weather forecast request failed")
        return "Погода: прогноз временно недоступен. Попробуйте позже."
    except (json.JSONDecodeError, ValueError):
        logging.getLogger(__name__).exception("Weather forecast request failed")
        return "Погода: прогноз временно недоступен. Попробуйте позже."


async def get_weather_text_async(user_id: int, timeout_sec: float = 1.5) -> str:
    try:
        return await asyncio.wait_for(asyncio.to_thread(get_weather_text, int(user_id)), timeout=timeout_sec)
    except asyncio.TimeoutError:
        cache_key = _weather_cache_key(user_id)
        with _CACHE_LOCK:
            ts_text = _WEATHER_CACHE.get(cache_key)
        if ts_text:
            return ts_text[1] + "\n\n⏱️ Показан последний сохранённый прогноз."
        return "Погода: запрос занимает слишком долго. Попробуйте позже."
