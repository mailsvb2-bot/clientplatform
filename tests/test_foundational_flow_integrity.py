from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.environment import is_production_env, normalize_app_env
from services.body import save_body_feedback
from services.db import db
from services.mood import create_session, get_user_session
from services.pending import clear_pending, consume_pending, peek_pending, set_pending
from services import weather


ROOT = Path(__file__).resolve().parents[1]


def test_production_alias_is_normalized_before_runtime_checks() -> None:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "APP_ENV": "production",
            "LOAD_DOTENV": "0",
            "BOT_TOKEN": "000000:TEST",
            "ADMIN_IDS": "1",
            "HEALTHCHECK_ENABLED": "1",
            "TELEGRAM_TRANSPORT": "polling",
            "TELEGRAM_WEBHOOK_ENABLED": "0",
            "MESSENGER_WEBHOOK_ENABLED": "0",
            "PAYMENT_HTTP_ENABLED": "0",
            "MAX_WEBHOOK_ENABLED": "0",
            "VK_WEBHOOK_ENABLED": "0",
            "METRO_DB_ENGINE": "postgres",
            "DATABASE_URL": "postgresql://ci:ci@127.0.0.1:5432/metrotherapy_ci_contract",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import config.settings as c; "
            "assert c.APP_ENV == 'prod'; assert os.environ['APP_ENV'] == 'prod'",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert normalize_app_env("production") == "prod"
    assert normalize_app_env("prod") == "prod"
    assert is_production_env("production") is True
    assert is_production_env("stage") is False


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


def test_mood_session_ownership_and_body_feedback_are_enforced_in_storage() -> None:
    owner_id = 92001
    foreign_id = 92002
    session_id = create_session(
        owner_id,
        kind="work",
        source="test",
        day="2026-07-27",
    )

    assert get_user_session(session_id, owner_id) is not None
    assert get_user_session(session_id, foreign_id) is None
    assert save_body_feedback(foreign_id, session_id, "forged", "Шея") is False

    assert save_body_feedback(owner_id, session_id, "forged", "Шея") is True
    assert save_body_feedback(owner_id, session_id, "forged", "Плечи") is True

    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, kind, area FROM body_feedback WHERE session_id=?",
            (session_id,),
        ).fetchall()

    assert len(rows) == 1
    assert int(rows[0]["user_id"]) == owner_id
    assert str(rows[0]["kind"]) == "work"
    assert str(rows[0]["area"]) == "Плечи"


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


def test_post_chart_has_one_canonical_router_owner() -> None:
    handlers_root = ROOT / "handlers"
    owners = []
    for path in handlers_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if 'F.data.regexp(r"^post:chart:\\d+$")' in source:
            owners.append(path.relative_to(ROOT).as_posix())

    assert owners == ["handlers/post_chart.py"]
    mood_router = (handlers_root / "mood.py").read_text(encoding="utf-8")
    assert "charts.router" not in mood_router
