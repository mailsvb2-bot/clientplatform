from __future__ import annotations
import logging


import json
from dataclasses import dataclass
from typing import Any

from services.db import db

from core.time_utils import utcnow_iso




@dataclass
class BodyQuestion:
    key: str
    question: str
    options: list[str]


def pick_body_question(force_key: str | None = None) -> BodyQuestion:
    """Возвращает случайный вопрос про напряжение в теле.

    Мы храним вопросы в micro_questions с ключами body_XX.
    Повторы разрешены (каждый раз может быть разный вопрос).
    """
    force_key = (force_key or "").strip() or None

    with db() as conn:
        if force_key:
            row = conn.execute(
                "SELECT key, question, options FROM micro_questions WHERE is_active=1 AND key=? LIMIT 1",
                (force_key,),
            ).fetchone()
        else:
            # Keep the LIKE pattern as a bound value. The DB compatibility layer translates
            # SQLite-style '?' placeholders for Postgres, and literal '%' inside SQL can be
            # misread by psycopg as an invalid placeholder. Parameterization works for both
            # SQLite and Postgres and keeps the query injection-safe.
            row = conn.execute(
                "SELECT key, question, options FROM micro_questions WHERE is_active=1 AND key LIKE ? ORDER BY RANDOM() LIMIT 1",
                ("body_%",),
            ).fetchone()
    if not row:
        return BodyQuestion(
            key='body_01',
            question='Где прямо сейчас больше всего чувствуется напряжение?',
            options=['Шея', 'Плечи', 'Челюсть', 'Поясница'],
        )
    try:
        opts = json.loads(row["options"])
    except (json.JSONDecodeError, TypeError, ValueError):
        logging.getLogger(__name__).exception("Failed to parse body question options, using fallback")
        opts = ['Шея', 'Плечи', 'Челюсть', 'Поясница']
    return BodyQuestion(key=str(row['key']), question=str(row['question']), options=[str(x) for x in (opts or [])])


def save_body_feedback(user_id: int, session_id: int, kind: str, area: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO body_feedback(session_id, user_id, kind, area, created_at_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(session_id, user_id) DO UPDATE SET
                kind=excluded.kind,
                area=excluded.area,
                created_at_utc=excluded.created_at_utc
            """,
            (int(session_id), int(user_id), str(kind), str(area), utcnow_iso()),
        )


def quick_technique(area: str) -> str:
    """Возвращает локальную безопасную технику саморегуляции (60–90 секунд).

    Пользовательский Telegram-флоу не должен зависеть от внешнего AI-провайдера:
    ответ обязан быть быстрым, предсказуемым и совместимым с ai_user_therapy_allowed=false.
    """
    area = (area or '').strip()

    # Локальный безопасный ответ: без внешнего AI, без сетевого вызова, без задержки провайдера.
    area_low = area.lower()
    lead = f"Мини‑техника на 60 секунд для зоны: {area}." if area else "Мини‑техника на 60 секунд."
    if "ше" in area_low or "плеч" in area_low:
        steps = [
            "1) Слегка опустите плечи на 1–2 мм — не вниз, а как будто " +
            "«отпускаете их на выдохе».",
            "2) Сделайте 3 медленных выдоха чуть длиннее вдоха.",
            "3) На каждом выдохе мягко «удлините шею» — макушкой вверх, подбородок чуть назад.",
            "4) Лёгкое круговое движение плечами на 1–2 см (очень маленькое).",
            "5) Заметьте, где стало хотя бы на 1% свободнее — и удержите это ощущение 5 секунд.",
        ]
    elif "челю" in area_low:
        steps = [
            "1) Проверьте: верхние и нижние зубы могут быть разомкнуты.",
            "2) Язык мягко лежит на нёбе за верхними зубами.",
            "3) 3 выдоха чуть длиннее вдоха.",
            "4) На выдохе слегка «расплавьте» уголки челюсти — на 1–2 мм.",
            "5) Найдите положение, где легче, и задержитесь в нём на 5–10 секунд.",
        ]
    elif "пояс" in area_low or "спин" in area_low:
        steps = [
            "1) Сделайте микродвижение тазом: 2 мм вперёд‑назад, найдите нейтраль.",
            "2) На выдохе слегка «расправьте» поясницу — без прогиба.",
            "3) 3 цикла: вдох — внимание в пояснице, выдох — «отпускаю на 1%».",
            "4) Мягко активируйте пресс на 10% и отпустите.",
            "5) Отметьте, что стало устойчивее хотя бы на 1%.",
        ]
    else:
        steps = [
            "1) Найдите это место в теле вниманием (без оценки).",
            "2) 3 спокойных вдоха/выдоха, выдох чуть длиннее.",
            "3) На каждом выдохе мысленно скажите: «на 1% мягче».",
            "4) Сделайте одно микродвижение, которое хочется (1–2 см), и остановитесь.",
            "5) Зафиксируйте, где стало легче, на 5 секунд.",
        ]

    return lead + "\n\n" + "\n".join(steps)


# Backward-compatible alias used by handlers.
# Установка A (контракты важнее кода): не ломаем импорты.
def technique_for_area(area: str) -> str:
    return quick_technique(area)
