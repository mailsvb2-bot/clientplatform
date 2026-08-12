from __future__ import annotations

"""Goal-first Brand DNA UX with confirmed website discovery and manual edits."""

import asyncio
from dataclasses import asdict

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.creative_studio_publication import (
    load_goal_visual_brand,
    save_goal_visual_brand,
)
from clientplatform.application.visual_brand_discovery import (
    VisualBrandDiscoveryError,
    discover_brand_from_website,
)
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.domain.visual_brand import TenantBrandDNA

from . import clientplatform_control as control


router = Router(name="clientplatform_visual_brand")


class VisualBrandState(StatesGroup):
    waiting_website = State()
    waiting_manual = State()
    confirming = State()


def _keyboard(business_token: str):
    return control._keyboard(
        [
            [("🌐 Взять стиль с сайта", f"cpb:site:{business_token}")],
            [("✏️ Изменить вручную", f"cpb:manual:{business_token}")],
            [("🏠 На главную", f"cpj:home:{business_token}")],
        ]
    )


def _proposal_keyboard(business_token: str):
    return control._keyboard(
        [
            [("✅ Применить", f"cpb:apply:{business_token}")],
            [("❌ Не менять", f"cpb:cancel:{business_token}")],
        ]
    )


def _brand_text(brand: TenantBrandDNA) -> str:
    value = brand.normalized()
    keywords = ", ".join(value.visual_keywords) if value.visual_keywords else "не заданы"
    tone = ", ".join(value.tone) if value.tone else "не задан"
    name = value.display_name or "не задано"
    return (
        f"Название: {name}\n"
        f"Тон: {tone}\n"
        f"Визуальный стиль: {keywords}\n"
        f"Цвета: {value.primary_color} · {value.accent_color} · {value.text_color}"
    )


def _business_id(token: str) -> str:
    return control._token_uuid(str(token or "").strip())


def _brand_from_state(data: dict, business_id: str) -> TenantBrandDNA:
    raw = data.get("brand_proposal")
    if not isinstance(raw, dict):
        raise ValueError("brand proposal is unavailable")
    return TenantBrandDNA(
        business_id=business_id,
        display_name=str(raw.get("display_name") or ""),
        tone=tuple(raw.get("tone") or ()),
        visual_keywords=tuple(raw.get("visual_keywords") or ()),
        forbidden_visuals=tuple(raw.get("forbidden_visuals") or ()),
        primary_color=str(raw.get("primary_color") or ""),
        accent_color=str(raw.get("accent_color") or ""),
        text_color=str(raw.get("text_color") or ""),
    ).normalized()


def _manual_brand(current: TenantBrandDNA, text: str) -> TenantBrandDNA:
    values: dict[str, str] = {}
    aliases = {
        "название": "display_name",
        "имя": "display_name",
        "основной": "primary_color",
        "основной цвет": "primary_color",
        "акцент": "accent_color",
        "акцентный": "accent_color",
        "акцентный цвет": "accent_color",
        "текст": "text_color",
        "цвет текста": "text_color",
        "стиль": "visual_keywords",
        "визуальный стиль": "visual_keywords",
    }
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = aliases.get(" ".join(key.casefold().split()))
        if field and value.strip():
            values[field] = value.strip()
    if not values:
        raise ValueError("brand manual fields were not recognized")
    keywords = current.visual_keywords
    if "visual_keywords" in values:
        keywords = tuple(
            item.strip()
            for item in values["visual_keywords"].replace(";", ",").split(",")
            if item.strip()
        )
    return TenantBrandDNA(
        business_id=current.business_id,
        display_name=values.get("display_name", current.display_name),
        tone=current.tone,
        visual_keywords=keywords,
        # Manual UI can add visual language and palette, but cannot silently
        # remove the trust/safety exclusions carried by the persisted profile.
        forbidden_visuals=current.forbidden_visuals,
        primary_color=values.get("primary_color", current.primary_color),
        accent_color=values.get("accent_color", current.accent_color),
        text_color=values.get("text_color", current.text_color),
    ).normalized()


@router.callback_query(F.data.startswith("cpb:open:"))
async def open_visual_brand(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        business_id = _business_id(token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось открыть фирменный стиль", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        "🎨 Фирменный стиль ClientPlatform\n\n"
        f"{_brand_text(brand)}\n\n"
        "Этот профиль используется при подготовке Creative Studio. Можно безопасно "
        "предложить настройки по публичному сайту или изменить основные поля вручную.",
        reply_markup=_keyboard(token),
    )


@router.callback_query(F.data.startswith("cpb:site:"))
async def ask_brand_website(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        business_id = _business_id(token)
        await control._actor(int(callback.from_user.id), business_id)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer("Бизнес недоступен", show_alert=True)
        return
    await state.set_state(VisualBrandState.waiting_website)
    await state.set_data({"brand_business_id": business_id, "brand_business_token": token})
    await callback.answer()
    await control._callback_message(callback).answer(
        "Пришлите адрес публичного сайта, например https://example.ru.\n\n"
        "ClientPlatform прочитает только ограниченный объём публичной HTML-страницы, "
        "покажет найденные название/цвета/визуальные признаки и ничего не сохранит "
        "без вашего подтверждения. Внутренние и локальные адреса блокируются."
    )


@router.message(VisualBrandState.waiting_website)
async def receive_brand_website(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        business_id = str(data["brand_business_id"])
        token = str(data["brand_business_token"])
        actor = await control._actor(control._user_id(message), business_id)
        current = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        suggestion = await asyncio.to_thread(
            discover_brand_from_website,
            business_id=business_id,
            website_url=str(message.text or "").strip(),
            current=current,
        )
    except KeyError:
        await state.clear()
        await message.answer("Сессия настройки устарела. Откройте «Фирменный стиль» ещё раз.")
        return
    except VisualBrandDiscoveryError:
        await message.answer(
            "Не удалось безопасно прочитать этот сайт. Нужен публичный HTTP(S)-адрес "
            "без логина, нестандартного порта и переходов во внутреннюю сеть."
        )
        return
    except (TypeError, ValueError, TenantPermissionDenied):
        await message.answer("Не удалось подготовить предложение по этому сайту.")
        return
    if not suggestion.has_changes:
        await state.clear()
        await message.answer(
            "На странице не нашёл достаточно надёжных новых сигналов, поэтому текущий "
            "Brand DNA оставил без изменений.",
            reply_markup=_keyboard(token),
        )
        return
    await state.update_data(
        brand_proposal=asdict(suggestion.brand),
        brand_proposal_source=suggestion.source_url,
        brand_proposal_evidence=list(suggestion.evidence),
    )
    await state.set_state(VisualBrandState.confirming)
    evidence = ", ".join(suggestion.evidence) or "публичные метаданные"
    await message.answer(
        "Нашёл предложение по фирменному стилю:\n\n"
        f"{_brand_text(suggestion.brand)}\n\n"
        f"Основание: {evidence}.\n"
        "Пока это только предложение — в профиль ничего не записано.",
        reply_markup=_proposal_keyboard(token),
    )


@router.callback_query(F.data.startswith("cpb:manual:"))
async def ask_manual_brand(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        business_id = _business_id(token)
        await control._actor(int(callback.from_user.id), business_id)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer("Бизнес недоступен", show_alert=True)
        return
    await state.set_state(VisualBrandState.waiting_manual)
    await state.set_data({"brand_business_id": business_id, "brand_business_token": token})
    await callback.answer()
    await control._callback_message(callback).answer(
        "Пришлите только те поля, которые хотите изменить. Например:\n\n"
        "Название: Моя практика\n"
        "Основной цвет: #172033\n"
        "Акцент: #E9C46A\n"
        "Цвет текста: #FFFFFF\n"
        "Стиль: calm, editorial, human\n\n"
        "Сначала покажу результат; сохранение будет отдельной кнопкой."
    )


@router.message(VisualBrandState.waiting_manual)
async def receive_manual_brand(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        business_id = str(data["brand_business_id"])
        token = str(data["brand_business_token"])
        actor = await control._actor(control._user_id(message), business_id)
        current = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        proposal = _manual_brand(current, str(message.text or ""))
    except KeyError:
        await state.clear()
        await message.answer("Сессия настройки устарела. Откройте «Фирменный стиль» ещё раз.")
        return
    except (TypeError, ValueError, TenantPermissionDenied):
        await message.answer(
            "Не смог распознать настройки. Цвета нужны в формате #RRGGBB, а поля — "
            "как в примере выше."
        )
        return
    await state.update_data(brand_proposal=asdict(proposal), brand_proposal_source="manual")
    await state.set_state(VisualBrandState.confirming)
    await message.answer(
        "Предлагаемые настройки:\n\n"
        f"{_brand_text(proposal)}\n\n"
        "Изменения ещё не сохранены.",
        reply_markup=_proposal_keyboard(token),
    )


@router.callback_query(F.data.startswith("cpb:apply:"))
async def apply_visual_brand(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if str(data.get("brand_business_token") or "") != token:
        await callback.answer("Это предложение уже устарело", show_alert=True)
        return
    try:
        business_id = _business_id(token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        proposal = _brand_from_state(data, business_id)
        saved = await asyncio.to_thread(save_goal_visual_brand, actor=actor, brand=proposal)
    except TenantPermissionDenied:
        await callback.answer(
            "Сохранять фирменный стиль может владелец или администратор",
            show_alert=True,
        )
        return
    except (TypeError, ValueError):
        await callback.answer("Не удалось сохранить фирменный стиль", show_alert=True)
        return
    await state.clear()
    await callback.answer("Сохранено")
    await control._callback_message(callback).answer(
        "✅ Brand DNA сохранён и будет участвовать в identity следующих генераций.\n\n"
        f"{_brand_text(saved)}",
        reply_markup=_keyboard(token),
    )


@router.callback_query(F.data.startswith("cpb:cancel:"))
async def cancel_visual_brand(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    await state.clear()
    await callback.answer("Изменения не сохранены")
    await control._callback_message(callback).answer(
        "Оставил текущий фирменный стиль без изменений.",
        reply_markup=_keyboard(token),
    )


__all__ = ["router"]
