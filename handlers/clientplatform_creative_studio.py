from __future__ import annotations

"""Discoverable owner image/video creation over the canonical visual gateway."""

import asyncio
import logging
import os
import tempfile
from types import ModuleType
from typing import Callable, cast

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from clientplatform.application.creative_generation import (
    abandon_ambiguous_creative_generation,
    abandon_creative_generation,
    authorize_creative_generation_redelivery,
    begin_creative_generation_submission,
    claim_creative_generation_delivery,
    get_active_creative_generation,
    get_creative_generation,
    mark_creative_generation_delivered,
    prepare_creative_generation,
    remember_creative_generation_job,
)
from clientplatform.application.creative_studio_publication import load_goal_visual_brand
from clientplatform.application.event_content_assets import (
    EventContentAssetError,
    store_generated_event_content_asset,
)
from clientplatform.application.visual_scene_planning import plan_visual_scene_contract
from clientplatform.application.visual_scene_variants import (
    VisualSceneVariant,
    build_visual_scene_variants,
    recommended_scene_variant,
    supplement_scene_variant,
)
from clientplatform.application.visual_style_preferences import (
    clear_visual_style_preference,
    load_visual_style_preference,
    save_visual_style_preference,
)
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_business_image_from_frozen_payload,
    create_business_visual_from_frozen_payload,
    freeze_business_image_payload,
    freeze_business_video_payload,
    frozen_business_visual_binding,
    frozen_business_visual_kind,
    frozen_business_visual_semantic_qa,
    frozen_business_visual_style,
    materialize_ad_visual,
    normalize_business_image_request,
    poll_ad_visual,
    review_business_image_semantics_from_frozen_payload,
    visual_generation_ready,
    visual_video_generation_mode,
    wait_ad_visual,
)
from clientplatform.domain.creative_generation import (
    CreativeGenerationReceipt,
    CreativeGenerationReceiptStatus,
)
from clientplatform.domain.event_content import EventContentStage
from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.domain.visual_prompt_compiler import semantic_flags_for_request
from clientplatform.domain.visual_scene_contract import VisualSceneContract
from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    infer_visual_style_intent,
    resolve_visual_style_intent,
)
from clientplatform.presentation import owner_navigation as nav
from clientplatform.presentation.visual_generation import (
    visual_failure_message,
    visual_provider_unavailable_message,
)
from clientplatform.presentation.visual_style import (
    style_choice,
    style_dashboard_rows,
    style_dashboard_text,
    style_dimension,
    style_dimension_rows,
    style_preset_name,
)

from . import clientplatform_control as control


logger = logging.getLogger(__name__)


router = Router(name="clientplatform_creative_studio")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformCreativeStudioState(StatesGroup):
    waiting_prompt = State()
    choosing_style = State()
    waiting_scene_supplement = State()


def _scene_variant_text(variants: tuple[VisualSceneVariant, ...]) -> str:
    chunks = [
        "🎬 Варианты постановки\n\n"
        "Смысл исходного запроса у всех вариантов одинаковый — меняется только "
        "способ его визуально показать. Можно выбрать готовый вариант или "
        "дополнить понравившийся своим уточнением."
    ]
    for index, variant in enumerate(variants, start=1):
        chunks.append(
            f"\n{index}. {variant.title}\n{variant.description}"
        )
    return "\n".join(chunks)


def _scene_variant_rows(
    token: str,
    variants: tuple[VisualSceneVariant, ...],
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    for index, variant in enumerate(variants, start=1):
        rows.append(
            [
                (f"✅ {index}. {variant.title}", f"cpc:sv:pick:{variant.id}:{token}"),
                ("✍️ Дополнить своим", f"cpc:sv:add:{variant.id}:{token}"),
            ]
        )
    rows.extend(
        [
            [("🤖 Выбрать лучший автоматически", f"cpc:sv:auto:{token}")],
            [("🎨 К настройкам стиля", f"cpc:st:open:{token}")],
        ]
    )
    return rows


def _scene_contract_from_state(data: dict) -> VisualSceneContract:
    return VisualSceneContract.from_mapping(data.get("creative_scene_contract"))


def _scene_variants_from_state(data: dict) -> tuple[VisualSceneVariant, ...]:
    raw = data.get("creative_scene_variants")
    if not isinstance(raw, list):
        raise ValueError("visual scene variants are unavailable")
    variants = tuple(VisualSceneVariant.from_mapping(item) for item in raw)
    if len(variants) != 5:
        raise ValueError("visual scene variants are unavailable")
    return variants


async def _ensure_scene_variants(
    state: FSMContext,
    data: dict,
) -> tuple[VisualSceneContract, str, tuple[VisualSceneVariant, ...]]:
    try:
        contract = _scene_contract_from_state(data)
        variants = _scene_variants_from_state(data)
        source = str(data.get("creative_scene_planner_source") or "").strip().lower()
        if source not in {"ai", "deterministic"}:
            raise ValueError("visual scene planner source is invalid")
        return contract, source, variants
    except (TypeError, ValueError):
        pass

    request = normalize_business_image_request(str(data["creative_pending_prompt"]))
    style = _style_intent_from_state(data)
    flags = semantic_flags_for_request(request)
    contract, source = await asyncio.to_thread(
        plan_visual_scene_contract,
        request=request,
        semantic_flags=flags,
    )
    variants = await asyncio.to_thread(
        build_visual_scene_variants,
        request=request,
        scene_contract=contract,
        style_intent=style,
    )
    await state.update_data(
        creative_scene_contract=contract.to_mapping(),
        creative_scene_planner_source=source,
        creative_scene_variants=[item.to_mapping() for item in variants],
    )
    return contract, source, variants


def _variant_by_id(
    variants: tuple[VisualSceneVariant, ...],
    variant_id: str,
) -> VisualSceneVariant:
    for variant in variants:
        if variant.id == variant_id:
            return variant
    raise ValueError("visual scene variant is unavailable")


def _receipt_kind(receipt: CreativeGenerationReceipt | None) -> str:
    if receipt is None:
        return "image"
    payload = str(getattr(receipt, "provider_payload_json", "") or "").strip()
    if not payload:
        return "image"
    try:
        return frozen_business_visual_kind(payload)
    except (TypeError, ValueError):
        return "image"


def _receipt_noun(receipt: CreativeGenerationReceipt | None) -> str:
    return "видео" if _receipt_kind(receipt) == "video" else "картинка"


def _receipt_semantic_qa_enabled(receipt: CreativeGenerationReceipt) -> bool:
    payload = str(getattr(receipt, "provider_payload_json", "") or "").strip()
    if not payload:
        return False
    try:
        return frozen_business_visual_semantic_qa(payload) is not None
    except (TypeError, ValueError):
        return False


def _semantic_qa_warning(qa) -> str:
    if qa is None or str(getattr(qa, "status", "") or "") != "needs_review":
        return ""
    issues = tuple(
        " ".join(str(item or "").split()).strip()
        for item in getattr(qa, "issues", ())
        if " ".join(str(item or "").split()).strip()
    )[:3]
    if not issues:
        return (
            "⚠️ Автопроверка смысла нашла сомнение в соответствии запросу. "
            "Новую генерацию я не запускала."
        )
    details = "\n".join(f"• {item}" for item in issues)
    return (
        "⚠️ Автопроверка смысла: картинка может передавать запрос не полностью.\n"
        + details
        + "\n\nНовую генерацию я не запускала."
    )


def _studio_navigation_rows(token: str) -> list[list[tuple[str, str]]]:
    return [
        [(nav.BACK.label, f"cpo:content:{token}")],
        [("🏠 В главное меню", f"cpj:home:{token}")],
    ]


def _menu_rows(
    token: str,
    active: CreativeGenerationReceipt | None = None,
    *,
    image_ready: bool = True,
    video_ready: bool = True,
    video_mode: str = "native",
):
    rows: list[list[tuple[str, str]]] = []
    if active is not None:
        label = (
            "⚠️ Проверить доставку"
            if active.delivery_claimed_at
            else (
                (
                    "✅ Получить готовое видео"
                    if _receipt_kind(active) == "video"
                    else "✅ Получить готовую картинку"
                )
                if active.status == CreativeGenerationReceiptStatus.SUCCEEDED
                else "🔄 Продолжить создание"
            )
        )
        action = "generate" if active.status in {
            CreativeGenerationReceiptStatus.PREPARED,
            CreativeGenerationReceiptStatus.SUBMITTING,
        } else "check"
        rows.append([(label, _receipt_callback(action, token, active))])
        if active.status == CreativeGenerationReceiptStatus.PREPARED:
            active_kind = _receipt_kind(active)
            edit_callback = (
                f"cpc:video:{token}"
                if active_kind == "video"
                else f"cpc:new:{token}"
            )
            alternate_label = (
                "✨ Вместо этого картинка"
                if active_kind == "video"
                else "🎬 Вместо этого видео"
            )
            alternate_callback = (
                f"cpc:new:{token}"
                if active_kind == "video"
                else f"cpc:video:{token}"
            )
            rows.append([("✏️ Изменить описание", edit_callback)])
            rows.append([(alternate_label, alternate_callback)])
    else:
        rows.append([
            (
                "✨ Создать картинку" if image_ready else "⚠️ Картинки недоступны",
                f"cpc:new:{token}" if image_ready else f"cpc:status:{token}:image",
            )
        ])
        rows.append([
            (
                (
                    "🎬 Создать AI-видео"
                    if video_mode == "native"
                    else "🎞 Оживить картинку"
                )
                if video_ready
                else "⚠️ Видео недоступно",
                f"cpc:video:{token}" if video_ready else f"cpc:status:{token}:video",
            )
        ])
    rows.extend(
        [
            [("🚀 Картинка для рекламы", f"cpo:start:{token}")],
            [(
                "картинка для рекламы (возможность редактирования)",
                f"cpc:editad:image:{token}",
            )],
            [(
                "видео для рекламы (возможность редактирования)",
                f"cpc:editad:video:{token}",
            )],
            [("🎨 Фирменный стиль", f"cpb:open:{token}")],
            *_studio_navigation_rows(token),
        ]
    )
    return control._keyboard(rows)


def _result_rows(
    token: str,
    receipt: CreativeGenerationReceipt | None = None,
):
    rows: list[list[tuple[str, str]]] = []
    kind = _receipt_kind(receipt) if receipt is not None else ""
    if receipt is not None and receipt.source_job_id:
        rows.append(
            [("📥 Скачать файл", _receipt_callback("download", token, receipt))]
        )
    if receipt is not None:
        rows.extend(
            [
                [("👍 Подходит", _receipt_callback("accept", token, receipt))],
                [("🎨 Изменить стиль", _receipt_callback("restyle", token, receipt))],
                [("🔄 Другой вариант", _receipt_callback("variant", token, receipt))],
                [
                    (
                        "✏️ Изменить идею",
                        f"cpc:video:{token}" if kind == "video" else f"cpc:new:{token}",
                    )
                ],
            ]
        )
    rows.extend(
        [
            [
                (
                    "✨ Создать картинку"
                    if kind == "video"
                    else "✨ Создать ещё картинку",
                    f"cpc:new:{token}",
                )
            ],
            [
                (
                    "🎬 Создать ещё видео"
                    if kind == "video"
                    else "🎬 Создать видео",
                    f"cpc:video:{token}",
                )
            ],
            [("🚀 Перейти к рекламе", f"cpo:ads:{token}")],
            *_studio_navigation_rows(token),
        ]
    )
    return control._keyboard(rows)


async def _actor_for_callback(callback: CallbackQuery, token: str):
    business_id = control._token_uuid(token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    actor.assert_can_manage_promotions()
    return actor


async def _active(actor) -> CreativeGenerationReceipt | None:
    return await asyncio.to_thread(get_active_creative_generation, actor=actor)


async def _retire_unavailable_completed_receipt(
    actor,
    receipt: CreativeGenerationReceipt | None,
    *,
    job=None,
) -> bool:
    """Retire a completed receipt only when its transient bytes are definitively gone.

    Generated media is intentionally stored only in transient storage. A provider or
    gateway restart can therefore leave durable SUCCEEDED metadata after the bytes
    have disappeared. Keeping that receipt active deadlocks the studio: every new
    image/video request is redirected back to an unrecoverable result.

    We only retire after an authoritative poll says the exact paid job succeeded but
    no asset is available. Transport/poll failures remain ambiguous and are preserved
    so a retry can never silently create another paid generation.
    """

    if (
        receipt is None
        or receipt.status != CreativeGenerationReceiptStatus.SUCCEEDED
        or not receipt.source_job_id
    ):
        return False

    current_job = job
    if current_job is None:
        try:
            current_job = await asyncio.to_thread(
                poll_ad_visual,
                job_id=receipt.source_job_id,
                scope_id=actor.business_id,
            )
        except (VisualCreativeError, ValueError):
            return False

    if (
        str(getattr(current_job, "status", "") or "") != "succeeded"
        or bool(getattr(current_job, "asset_ready", False))
    ):
        return False

    try:
        return bool(
            await asyncio.to_thread(
                abandon_creative_generation,
                actor=actor,
                receipt_id=receipt.id,
            )
        )
    except LookupError:
        # A concurrent completion/cleanup already made the stale receipt terminal.
        return True


def _receipt_callback(
    action: str, token: str, receipt: CreativeGenerationReceipt
) -> str:
    callback = (
        f"cpc:{action}:{token}:{control._uuid_token(receipt.id)}"
    )
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("creative generation callback is too long")
    return callback


async def _receipt_for_callback(actor, receipt_token: str) -> CreativeGenerationReceipt:
    receipt_id = control._token_uuid(receipt_token)
    return await asyncio.to_thread(
        get_creative_generation,
        actor=actor,
        receipt_id=receipt_id,
    )


async def send_creative_studio_menu(
    message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    actor.assert_can_manage_promotions()
    active = await _active(actor)
    if await _retire_unavailable_completed_receipt(actor, active):
        active = await _active(actor)
    token = control._uuid_token(business_id)
    country_code = os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "")
    try:
        image_ready, video_mode = await asyncio.gather(
            asyncio.to_thread(
                visual_generation_ready,
                kind="image",
                country_code=country_code,
            ),
            asyncio.to_thread(
                visual_video_generation_mode,
                country_code=country_code,
            ),
        )
        video_ready = video_mode != "unavailable"
    except VisualCreativeError:
        image_ready = False
        video_ready = False
        video_mode = "unavailable"
    video_status = (
        "✅ полноценная AI-генерация"
        if video_mode == "native"
        else "🟡 доступно оживление AI-кадра"
        if video_mode == "motion"
        else "⚠️ генератор недоступен"
    )
    status_lines = (
        f"Картинки: {'✅ генератор подключён' if image_ready else '⚠️ генератор недоступен'}\n"
        f"Видео: {video_status}"
    )
    if active is None:
        body = (
            "🎨 Картинки и видео\n\n"
            + status_lines
            + "\n\nВыберите, что хотите создать — картинку или короткое видео. "
            "Опишите результат обычными словами: "
            "ClientPlatform сам использует доступный генератор и сохранённый "
            "фирменный стиль бизнеса — выбирать модель вручную не нужно."
        )
    elif active.status == CreativeGenerationReceiptStatus.PREPARED:
        qa_disclosure = (
            "\n\nПосле готовой картинки ClientPlatform один раз выполнит отдельную "
            "AI-проверку смысла. Она не создаёт новую картинку и не повторяется "
            "при повторной проверке или доставке результата."
            if _receipt_kind(active) == "image"
            and _receipt_semantic_qa_enabled(active)
            else ""
        )
        body = (
            "🎨 Картинки и видео\n\n"
            "У Вас уже подготовлен запрос:\n"
            f"{active.request_text}\n\n"
            + (
                "Платные AI-вызовы ещё не начинались. Кнопка «Продолжить создание» "
                "подтверждает генерацию и один отдельный QA-вызов."
                if _receipt_kind(active) == "image"
                and _receipt_semantic_qa_enabled(active)
                else "Платный AI-вызов ещё не начинался."
            )
            + qa_disclosure
            + "\n\nМожно продолжить или изменить описание."
        )
    elif active.delivery_claimed_at:
        delivery_subject = (
            "готового видео"
            if _receipt_kind(active) == "video"
            else "готовой картинки"
        )
        body = (
            "🎨 Картинки и видео\n\n"
            f"Отправка {delivery_subject} уже начиналась и могла завершиться. "
            "ClientPlatform не повторит её автоматически, чтобы не прислать дубль."
        )
    else:
        body = (
            "🎨 Картинки и видео\n\n"
            "У Вас уже есть незавершённая генерация. ClientPlatform продолжит именно "
            "её — новый платный job автоматически не создаётся."
        )
    await message.answer(
        body,
        reply_markup=_menu_rows(
            token,
            active,
            image_ready=image_ready,
            video_ready=video_ready,
            video_mode=video_mode,
        ),
    )


@router.callback_query(F.data.startswith("cpc:status:"))
async def creative_provider_status(callback: CallbackQuery) -> None:
    _, _, token, kind = str(callback.data).split(":", 3)
    try:
        await _actor_for_callback(callback, token)
        country_code = os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "")
        if kind == "video":
            video_mode = await asyncio.to_thread(
                visual_video_generation_mode,
                country_code=country_code,
            )
            ready = video_mode != "unavailable"
        else:
            video_mode = ""
            ready = await asyncio.to_thread(
                visual_generation_ready,
                kind=kind,
                country_code=country_code,
            )
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось проверить генератор", show_alert=True)
        return
    except VisualCreativeError:
        await callback.answer("Не удалось проверить генератор", show_alert=True)
        return

    if kind == "video" and ready:
        status_text = (
            "✅ Полноценная AI-генерация видео подключена."
            if video_mode == "native"
            else "🟡 Сейчас доступно только оживление AI-кадра, а не генерация движущейся сцены."
        )
    else:
        noun = "видео" if kind == "video" else "картинок"
        status_text = (
            f"✅ Генератор {noun} подключён и доступен."
            if ready
            else f"⚠️ Генератор {noun} сейчас не подключён к production-шлюзу. "
            "Платный AI-вызов не будет запущен."
        )
    await callback.answer()
    await control._callback_message(callback).answer(
        status_text,
        reply_markup=control._keyboard(
            [[("⬅️ К картинкам и видео", f"cpc:open:{token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cpc:open:"))
async def open_creative_studio(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание визуалов недоступно для Вашей роли", show_alert=True
        )
        return
    await state.clear()
    await callback.answer()
    await send_creative_studio_menu(
        control._callback_message(callback),
        user_id=actor.user_id,
        business_id=actor.business_id,
    )


@router.callback_query(F.data.startswith("cpc:editad:"))
async def open_editable_advertising(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        _, _, kind, token = str(callback.data).split(":", 3)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if kind not in {"image", "video"}:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    try:
        await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание рекламы недоступно для Вашей роли",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer()
    noun = "видео" if kind == "video" else "картинки"
    await control._callback_message(callback).answer(
        f"Редактируемый режим {noun} работает внутри рекламного черновика: "
        "AI создаёт визуальную основу, а заголовок, текст и CTA остаются отдельной "
        "детерминированной композицией. Правки макета не запускают новый AI-job.\n\n"
        "Сначала подготовьте рекламный черновик; в нём будут отдельные кнопки "
        "редактируемой картинки и видео.",
        reply_markup=control._keyboard(
            [[("🚀 Подготовить рекламу", f"cpo:start:{token}")]]
        ),
    )


async def _ask_creative_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    kind: str,
) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        actor = await _actor_for_callback(callback, token)
        active = await _active(actor)
        if await _retire_unavailable_completed_receipt(actor, active):
            active = await _active(actor)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание визуалов недоступно для Вашей роли", show_alert=True
        )
        return
    if active is not None and active.status != CreativeGenerationReceiptStatus.PREPARED:
        try:
            target = control._callback_message(callback)
        except ValueError:
            await callback.answer(
                "Сначала продолжите уже начатую генерацию",
                show_alert=True,
            )
            return
        await callback.answer()
        await target.answer(
            "У Вас уже есть незавершённая генерация. Продолжите её или завершите "
            "этот результат, чтобы начать новый.",
            reply_markup=_menu_rows(token, active),
        )
        return

    video_mode = ""
    if kind == "video":
        try:
            video_mode = await asyncio.to_thread(
                visual_video_generation_mode,
                country_code=os.getenv("VISUAL_DEPLOYMENT_COUNTRY", ""),
            )
        except VisualCreativeError:
            await callback.answer("Не удалось проверить генератор видео", show_alert=True)
            return
        if video_mode == "unavailable":
            await callback.answer(
                "Видео сейчас недоступно: рабочий production-генератор не подключён.",
                show_alert=True,
            )
            return

    await state.set_state(ClientPlatformCreativeStudioState.waiting_prompt)
    await state.set_data(
        {
            "creative_business_id": actor.business_id,
            "creative_business_token": token,
            "creative_kind": kind,
        }
    )
    await callback.answer()
    target = control._callback_message(callback)
    if kind == "video":
        if video_mode == "motion":
            prompt_text = (
                "Сейчас доступен безопасный резервный режим: ClientPlatform создаст "
                "AI-кадр и превратит его в короткий MP4 с плавным движением камеры. "
                "Это не генерация движущейся сцены. Опишите ключевой кадр обычными словами."
            )
        else:
            prompt_text = (
                "Опишите короткий ролик обычными словами, например: "
                "«спокойное вертикальное видео уютного кабинета психолога, "
                "мягкое движение камеры, естественный свет, без текста»."
            )
        await target.answer(
            "Какое видео создать?\n\n" + prompt_text,
            reply_markup=control._keyboard(_studio_navigation_rows(token)),
        )
    else:
        await target.answer(
            "Какую картинку создать?\n\n"
            "Напишите обычными словами, например: «спокойная реалистичная фотография "
            "кабинета психолога, светлая, без текста».",
            reply_markup=control._keyboard(_studio_navigation_rows(token)),
        )


@router.callback_query(F.data.startswith("cpc:new:"))
async def ask_creative_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_creative_prompt(callback, state, kind="image")


@router.callback_query(F.data.startswith("cpc:video:"))
async def ask_creative_video_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_creative_prompt(callback, state, kind="video")


def _style_intent_from_state(data: dict) -> VisualStyleIntent:
    raw = data.get("creative_style_intent")
    return VisualStyleIntent.from_mapping(raw if isinstance(raw, dict) else None)


def _style_session_matches(data: dict, token: str) -> bool:
    return bool(
        str(data.get("creative_business_token") or "") == str(token or "")
        and str(data.get("creative_business_id") or "").strip()
        and str(data.get("creative_pending_prompt") or "").strip()
        and str(data.get("creative_kind") or "") in {"image", "video"}
    )


async def _replace_or_answer(
    target: Message,
    text: str,
    *,
    reply_markup=None,
) -> None:
    """Prefer editing the current bot message so inline selection never walks the chat."""

    edit_text = getattr(target, "edit_text", None)
    if callable(edit_text):
        try:
            await edit_text(text, reply_markup=reply_markup)
            return
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).casefold():
                return
    await target.answer(text, reply_markup=reply_markup)


async def _show_style_dashboard(target: Message, data: dict, token: str) -> None:
    style = _style_intent_from_state(data)
    inferred = tuple(
        str(item)
        for item in data.get("creative_style_inferred_fields") or ()
        if str(item)
    )
    await _replace_or_answer(
        target,
        style_dashboard_text(
            style,
            request_inferred_fields=inferred,
            saved_applied=bool(data.get("creative_saved_style_applied")),
        ),
        reply_markup=control._keyboard(style_dashboard_rows(token, style)),
    )


async def _show_paid_generation_confirmation(
    target: Message,
    *,
    token: str,
    receipt: CreativeGenerationReceipt,
    replace: bool = False,
) -> None:
    noun = "видео" if _receipt_kind(receipt) == "video" else "картинку"
    qa_enabled = (
        _receipt_kind(receipt) == "image"
        and _receipt_semantic_qa_enabled(receipt)
    )
    edit_callback = (
        f"cpc:video:{token}"
        if _receipt_kind(receipt) == "video"
        else f"cpc:new:{token}"
    )
    text = (
        "✨ Всё готово к генерации\n\n"
        f"Задача: {receipt.request_text}\n\n"
        "ClientPlatform уже развернула короткое описание в подробное визуальное "
        "задание и зафиксировала выбранный стиль. Генерация может расходовать "
        "платную AI-квоту. "
        + (
            "Платные AI-вызовы начнутся только после кнопки ниже. "
            if qa_enabled
            else "Платный вызов начнётся только после кнопки ниже. "
        )
        + "Повторный запуск этого же задания использует тот же frozen brief и "
        "idempotency key."
        + (
            "\n\nПосле готовой картинки ClientPlatform может один раз выполнить "
            "отдельную AI-проверку смысла: видно ли действие/изменение, нет ли "
            "лишнего текста и явных физических ошибок. Это дополнительный AI-вызов, "
            "но он не создаёт новую картинку и не повторяется при проверке или "
            "повторной отправке результата."
            if qa_enabled
            else ""
        )
        + (
            "\n\nДля видео ClientPlatform сначала использует полноценный генератор "
            "движущейся сцены. Если такой провайдер недоступен до принятия задания, "
            "может быть использован явно обозначенный motion fallback из AI-кадра."
            if _receipt_kind(receipt) == "video"
            else ""
        )
    )
    reply_markup = control._keyboard(
        [
            [(f"✅ Создать 1 {noun}", _receipt_callback("generate", token, receipt))],
            [("✏️ Изменить описание", edit_callback)],
            [("⬅️ Не создавать", f"cpc:open:{token}")],
        ]
    )
    if replace:
        # Paid consent must always be a fresh, explicit message with its own
        # keyboard.  Reusing the large style-dashboard message proved fragile in
        # real Telegram clients: the text could be edited while the new inline
        # keyboard was not rendered, leaving no way to start the paid job.
        edit_reply_markup = getattr(target, "edit_reply_markup", None)
        if callable(edit_reply_markup):
            try:
                await edit_reply_markup(reply_markup=None)
            except TelegramAPIError:
                # A stale dashboard keyboard is harmless; the new consent message
                # below is the canonical action surface. Keep the failure visible
                # for transport diagnostics without blocking the owner action.
                logger.debug(
                    "Could not clear stale creative-style keyboard before paid confirmation",
                    exc_info=True,
                )
    await target.answer(text, reply_markup=reply_markup)


async def _prepare_styled_generation(
    target: Message,
    state: FSMContext,
    *,
    user_id: int,
    token: str,
    scene_variant: VisualSceneVariant | None = None,
) -> None:
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await target.answer(
            "Эта настройка уже устарела. Откройте «Картинки и креативы» ещё раз."
        )
        return
    try:
        business_id = str(data["creative_business_id"])
        kind = str(data["creative_kind"])
        prompt = normalize_business_image_request(str(data["creative_pending_prompt"]))
        brand_context = str(data.get("creative_brand_context") or "")
        country_code = str(data.get("creative_country_code") or "")
        style = _style_intent_from_state(data)
        scene_contract, planner_source, scene_variants = await _ensure_scene_variants(
            state,
            data,
        )
        selected_scene_variant = (
            scene_variant
            if scene_variant is not None
            else recommended_scene_variant(scene_variants)
        )
        actor = await control._actor(int(user_id), business_id)
        actor.assert_can_manage_promotions()
        freezer = (
            freeze_business_video_payload
            if kind == "video"
            else freeze_business_image_payload
        )
        provider_payload_json = freezer(
            request=prompt,
            brand_context=brand_context,
            country_code=country_code,
            style_intent=style,
            scene_contract=scene_contract,
            scene_planner_source=planner_source,
            scene_variant=selected_scene_variant,
        )
        receipt = await asyncio.to_thread(
            prepare_creative_generation,
            actor=actor,
            request_text=prompt,
            brand_context=brand_context,
            country_code=country_code,
            provider_payload_json=provider_payload_json,
        )
    except TenantPermissionDenied:
        await target.answer("Создание визуалов недоступно для Вашей роли.")
        return
    except OSError:
        await target.answer("Не удалось безопасно подготовить генерацию. Попробуйте позже.")
        return
    except (KeyError, TypeError, ValueError):
        await target.answer("Не удалось безопасно подготовить генерацию. Попробуйте позже.")
        return
    await state.clear()
    if receipt.request_text != prompt:
        await target.answer(
            "У Вас уже есть незавершённая генерация. Продолжите её — новый платный "
            "job автоматически не создаётся.",
            reply_markup=_menu_rows(token, receipt),
        )
        return
    await _show_paid_generation_confirmation(
        target,
        token=token,
        receipt=receipt,
        replace=True,
    )


@router.message(ClientPlatformCreativeStudioState.waiting_prompt)
async def receive_creative_prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    token = str(data.get("creative_business_token") or "").strip()
    try:
        business_id = str(data["creative_business_id"])
        token = str(data["creative_business_token"])
        kind = str(data.get("creative_kind") or "image").strip().lower()
        if kind not in {"image", "video"}:
            raise ValueError("unsupported creative kind")
        actor = await control._actor(control._user_id(message), business_id)
        actor.assert_can_manage_promotions()
        prompt = normalize_business_image_request(str(message.text or ""))
    except KeyError:
        await state.clear()
        await message.answer(
            "Сессия устарела. Откройте «Картинки и креативы» ещё раз.",
            reply_markup=(
                control._keyboard(_studio_navigation_rows(token))
                if token
                else None
            ),
        )
        return
    except (TypeError, ValueError, TenantPermissionDenied):
        await message.answer(
            "Опишите картинку или видео одним сообщением до 1500 символов. "
            "Технический промпт составлять не нужно.",
            reply_markup=control._keyboard(_studio_navigation_rows(token)),
        )
        return
    try:
        country_code = os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "")
        ready = await asyncio.to_thread(
            visual_generation_ready,
            kind=kind,
            country_code=country_code,
        )
        if not ready:
            await message.answer(
                visual_provider_unavailable_message(kind),
                reply_markup=control._keyboard(_studio_navigation_rows(token)),
            )
            return
        brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        brand_context = brand.prompt_context()
        saved = await asyncio.to_thread(load_visual_style_preference, actor=actor)
        inference = infer_visual_style_intent(prompt)
        resolved = resolve_visual_style_intent(
            request=prompt,
            saved=saved,
        )
    except VisualCreativeError:
        await message.answer(
            "Не удалось проверить доступность генератора. Платный запрос не запускался. "
            "Попробуйте ещё раз после восстановления шлюза генерации."
        )
        return
    except (OSError, ValueError):
        await message.answer("Не удалось безопасно подготовить генерацию. Попробуйте позже.")
        return

    saved_applied = any(
        value != "auto"
        for value in saved.to_mapping().values()
    )
    await state.update_data(
        creative_kind=kind,
        creative_pending_prompt=prompt,
        creative_brand_context=brand_context,
        creative_country_code=country_code,
        creative_style_intent=resolved.to_mapping(),
        creative_saved_style_applied=saved_applied,
        creative_style_inferred_fields=list(inference.explicit_fields),
    )
    await state.set_state(ClientPlatformCreativeStudioState.choosing_style)
    await message.answer(
        "Идею понял. Технический промпт писать не нужно — ClientPlatform сама "
        "разложит запрос на сцену, действия, взаимодействия, видимый результат, "
        "композицию, свет, детали и ограничения для генератора. Можно сразу отдать "
        "всё автоматике или при желании уточнить стиль кнопками.",
        reply_markup=control._keyboard(
            [
                [("🤖 Сделать всё автоматически", f"cpc:st:go:{token}")],
                [("🎬 Показать 5 вариантов", f"cpc:sv:show:{token}")],
                [("🎨 Уточнить стиль", f"cpc:st:open:{token}")],
                [
                    (
                        "✏️ Изменить идею",
                        f"cpc:video:{token}" if kind == "video" else f"cpc:new:{token}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpc:st:open:"))
async def open_visual_style(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    await callback.answer()
    await _show_style_dashboard(control._callback_message(callback), data, token)


@router.callback_query(F.data.startswith("cpc:st:p:"))
async def choose_visual_style_preset(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, _, preset_code, token = str(callback.data).split(":", 4)
        quick_style = style_preset_name(preset_code)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        style = _style_intent_from_state(data).with_quick_style(quick_style)
        selected = style.has_quick_style(quick_style)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(
        creative_style_intent=style.to_mapping(),
        creative_saved_style_applied=False,
    )
    data = await state.get_data()
    await callback.answer("Акцент добавлен" if selected else "Акцент снят")
    await _show_style_dashboard(control._callback_message(callback), data, token)


@router.callback_query(F.data.startswith("cpc:st:d:"))
async def open_visual_style_dimension(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, _, code, token = str(callback.data).split(":", 4)
        dimension = style_dimension(code)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    style = _style_intent_from_state(data)
    await callback.answer()
    await _replace_or_answer(
        control._callback_message(callback),
        f"🎨 {dimension.title}\n\nВыберите вариант. Текущий отмечен галочкой:",
        reply_markup=control._keyboard(style_dimension_rows(token, code, style)),
    )


@router.callback_query(F.data.startswith("cpc:st:s:"))
async def set_visual_style_dimension(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, _, dimension_code, value_code, token = str(callback.data).split(":", 5)
        field, value = style_choice(dimension_code, value_code)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    style = _style_intent_from_state(data).with_value(field, value)
    await state.update_data(
        creative_style_intent=style.to_mapping(),
        creative_saved_style_applied=False,
    )
    data = await state.get_data()
    style = _style_intent_from_state(data)
    dimension = style_dimension(dimension_code)
    await callback.answer("Настройка сохранена")
    await _replace_or_answer(
        control._callback_message(callback),
        f"🎨 {dimension.title}\n\nВыберите вариант. Текущий отмечен галочкой:",
        reply_markup=control._keyboard(
            style_dimension_rows(token, dimension_code, style)
        ),
    )


@router.callback_query(F.data.startswith("cpc:st:reset:"))
async def reset_visual_style(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    style = resolve_visual_style_intent(
        request=str(data["creative_pending_prompt"]),
    )
    await state.update_data(
        creative_style_intent=style.to_mapping(),
        creative_saved_style_applied=False,
    )
    data = await state.get_data()
    await callback.answer("Вернул автоматический стиль")
    await _show_style_dashboard(control._callback_message(callback), data, token)


@router.callback_query(F.data.startswith("cpc:st:save:"))
async def save_current_visual_style(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["creative_business_id"]),
        )
        style = _style_intent_from_state(data)
        await asyncio.to_thread(
            save_visual_style_preference,
            actor=actor,
            style=style,
        )
    except TenantPermissionDenied:
        await callback.answer("Настройка стиля недоступна для Вашей роли", show_alert=True)
        return
    except (KeyError, TypeError, ValueError):
        await callback.answer("Не удалось запомнить стиль", show_alert=True)
        return
    await state.update_data(creative_saved_style_applied=True)
    await callback.answer("Буду предлагать этот стиль в следующих визуалах")


@router.callback_query(F.data.startswith("cpc:st:clear:"))
async def clear_current_visual_style(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["creative_business_id"]),
        )
        await asyncio.to_thread(clear_visual_style_preference, actor=actor)
    except TenantPermissionDenied:
        await callback.answer("Настройка стиля недоступна для Вашей роли", show_alert=True)
        return
    except (KeyError, TypeError, ValueError):
        await callback.answer("Не удалось сбросить сохранённый стиль", show_alert=True)
        return
    await state.update_data(creative_saved_style_applied=False)
    await callback.answer("Сохранённый стиль сброшен")


async def _show_scene_variant_choices(
    target: Message,
    state: FSMContext,
    *,
    token: str,
) -> None:
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await target.answer(
            "Эта настройка уже устарела. Откройте «Картинки и креативы» ещё раз."
        )
        return
    try:
        _contract, _source, variants = await _ensure_scene_variants(state, data)
    except (KeyError, OSError, TypeError, ValueError):
        await target.answer(
            "Не удалось подготовить варианты постановки. Можно оставить "
            "«Автоматически» — исходный смысл всё равно останется обязательным."
        )
        return
    await state.set_state(ClientPlatformCreativeStudioState.choosing_style)
    await _replace_or_answer(
        target,
        _scene_variant_text(variants),
        reply_markup=control._keyboard(_scene_variant_rows(token, variants)),
    )


@router.callback_query(F.data.startswith("cpc:sv:show:"))
async def show_scene_variants(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    await callback.answer("Готовлю варианты постановки…")
    await _show_scene_variant_choices(
        control._callback_message(callback),
        state,
        token=token,
    )


@router.callback_query(F.data.startswith("cpc:sv:auto:"))
async def auto_scene_variant(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        _contract, _source, variants = await _ensure_scene_variants(state, data)
        selected = recommended_scene_variant(variants)
    except (KeyError, OSError, TypeError, ValueError):
        await callback.answer("Не удалось выбрать вариант", show_alert=True)
        return
    await callback.answer("Выбран лучший вариант")
    await _prepare_styled_generation(
        control._callback_message(callback),
        state,
        user_id=int(callback.from_user.id),
        token=token,
        scene_variant=selected,
    )


@router.callback_query(F.data.startswith("cpc:sv:pick:"))
async def pick_scene_variant(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, _, variant_id, token = str(callback.data).split(":", 4)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        selected = _variant_by_id(_scene_variants_from_state(data), variant_id)
    except (TypeError, ValueError):
        await callback.answer("Варианты устарели", show_alert=True)
        return
    await callback.answer("Вариант выбран")
    await _prepare_styled_generation(
        control._callback_message(callback),
        state,
        user_id=int(callback.from_user.id),
        token=token,
        scene_variant=selected,
    )


@router.callback_query(F.data.startswith("cpc:sv:add:"))
async def ask_scene_variant_supplement(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        _, _, _, variant_id, token = str(callback.data).split(":", 4)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    try:
        variant = _variant_by_id(_scene_variants_from_state(data), variant_id)
    except (TypeError, ValueError):
        await callback.answer("Варианты устарели", show_alert=True)
        return
    await state.update_data(creative_scene_selected_variant_id=variant.id)
    await state.set_state(ClientPlatformCreativeStudioState.waiting_scene_supplement)
    await callback.answer()
    await control._callback_message(callback).answer(
        "✍️ Дополнить своим\n\n"
        f"Вы выбрали: {variant.title}.\n"
        f"{variant.description}\n\n"
        "Напишите одним сообщением, что хотите добавить к этой постановке — "
        "например ракурс, освещение, окружение, настроение или важную визуальную "
        "деталь. Исходный смысл запроса останется обязательным. До 600 символов.",
        reply_markup=control._keyboard(
            [[("⬅️ К вариантам", f"cpc:sv:show:{token}")]]
        ),
    )


@router.message(ClientPlatformCreativeStudioState.waiting_scene_supplement)
async def receive_scene_variant_supplement(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    token = str(data.get("creative_business_token") or "").strip()
    try:
        variant_id = str(data["creative_scene_selected_variant_id"])
        variants = list(_scene_variants_from_state(data))
        base = _variant_by_id(tuple(variants), variant_id)
        updated = supplement_scene_variant(base, str(message.text or ""))
    except (KeyError, TypeError, ValueError):
        await message.answer(
            "Дополнение не удалось сохранить. Напишите уточнение одним сообщением "
            "до 600 символов или вернитесь к вариантам.",
            reply_markup=(
                control._keyboard([[("⬅️ К вариантам", f"cpc:sv:show:{token}")]])
                if token
                else None
            ),
        )
        return

    variants = [updated if item.id == updated.id else item for item in variants]
    await state.update_data(
        creative_scene_variants=[item.to_mapping() for item in variants],
        creative_scene_selected_variant_id=updated.id,
    )
    await state.set_state(ClientPlatformCreativeStudioState.choosing_style)
    await message.answer(
        "✅ Вариант дополнен\n\n"
        f"{updated.title}\n{updated.description}\n\n"
        "Можно использовать его сейчас, дополнить ещё или вернуться к пяти вариантам.",
        reply_markup=control._keyboard(
            [
                [("✅ Использовать этот вариант", f"cpc:sv:pick:{updated.id}:{token}")],
                [("✍️ Дополнить своим ещё", f"cpc:sv:add:{updated.id}:{token}")],
                [("🎬 Все 5 вариантов", f"cpc:sv:show:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpc:st:go:"))
async def confirm_visual_style(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).rsplit(":", 1)[-1]
    data = await state.get_data()
    if not _style_session_matches(data, token):
        await callback.answer("Эта настройка уже устарела", show_alert=True)
        return
    await callback.answer("ClientPlatform выбирает лучший вариант…")
    await _prepare_styled_generation(
        control._callback_message(callback),
        state,
        user_id=int(callback.from_user.id),
        token=token,
    )


def _owner_requested_copy_space(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").casefold().split())
    markers = (
        "место для текста",
        "место под текст",
        "свободное место",
        "пустое место",
        "copy space",
        "negative space",
        "space for text",
        "пространство для текста",
    )
    return any(marker in normalized for marker in markers)


async def _finish_visual(
    callback: CallbackQuery,
    *,
    actor,
    receipt: CreativeGenerationReceipt,
    job,
) -> bool:
    if str(getattr(job, "status", "") or "") != "succeeded" or not bool(
        getattr(job, "asset_ready", False)
    ):
        return False
    target = control._callback_message(callback)
    token = control._uuid_token(actor.business_id)
    try:
        with tempfile.TemporaryDirectory(prefix="clientplatform-creative-") as directory:
            if _owner_requested_copy_space(receipt.request_text):
                path = await asyncio.to_thread(
                    materialize_ad_visual,
                    job,
                    output_dir=directory,
                    repair_blank_bands=False,
                )
            else:
                path = await asyncio.to_thread(
                    materialize_ad_visual,
                    job,
                    output_dir=directory,
                )
            binding = frozen_business_visual_binding(receipt.provider_payload_json)
            claimed = await asyncio.to_thread(
                claim_creative_generation_delivery,
                actor=actor,
                receipt_id=receipt.id,
            )
            if not claimed:
                try:
                    latest = await asyncio.to_thread(
                        get_creative_generation,
                        actor=actor,
                        receipt_id=receipt.id,
                    )
                except LookupError:
                    await target.answer(
                        "Эта генерация уже была отправлена или завершена.",
                        reply_markup=_result_rows(token),
                    )
                    return True
                await target.answer(
                    "⚠️ Отправка этого визуала уже началась и могла завершиться. "
                    "Автоматически повторять её не буду, чтобы не прислать дубль.",
                    reply_markup=_delivery_recovery_rows(token, latest, ambiguous=True),
                )
                return True
            try:
                if str(getattr(job, "kind", "") or "") == "video":
                    caption = (
                        "✅ Оживлённая AI-картинка готова"
                        if str(getattr(job, "provider", "") or "") == "yandexart_motion"
                        else "✅ AI-видео готово"
                    )
                    await target.answer_video(
                        FSInputFile(path),
                        caption=caption,
                        supports_streaming=True,
                    )
                else:
                    await target.answer_photo(FSInputFile(path), caption="✅ Картинка готова")
            except TelegramAPIError:
                await target.answer(
                    "⚠️ Telegram не дал однозначного подтверждения доставки. "
                    "Автоматический повтор заблокирован, чтобы не прислать дубль.",
                    reply_markup=_delivery_recovery_rows(token, receipt, ambiguous=True),
                )
                return True
            if binding is not None and binding.get("type") == "event_content":
                try:
                    await asyncio.to_thread(
                        store_generated_event_content_asset,
                        actor=actor,
                        event_id=binding["event_id"],
                        stage=EventContentStage(binding["stage"]),
                        slot_key=binding["slot_key"],
                        kind=ContentKind(binding["kind"]),
                        path=path,
                        content_type=str(getattr(job, "mime_type", "") or (
                            "video/mp4" if binding["kind"] == "video" else "image/jpeg"
                        )),
                        extension=path.suffix.lower().lstrip(".") or (
                            "mp4" if binding["kind"] == "video" else "jpg"
                        ),
                        source_ref=receipt.id,
                    )
                except EventContentAssetError as exc:
                    # Delivery to the owner is the primary contract. A secondary
                    # campaign attachment failure must never hide a valid result.
                    logger.warning(
                        "creative event-content attachment failed after owner delivery: %s",
                        exc,
                    )
    except (VisualCreativeError, ValueError):
        await target.answer(
            "Генератор завершил визуал, но файл сейчас не удалось получить. "
            "Можно проверить файл ещё раз или завершить этот результат и создать новый.",
            reply_markup=_delivery_recovery_rows(token, receipt),
        )
        return True
    except OSError:
        await target.answer(
            "Генератор завершил визуал, но файл сейчас не удалось получить. "
            "Можно проверить файл ещё раз или завершить этот результат и создать новый.",
            reply_markup=_delivery_recovery_rows(token, receipt),
        )
        return True
    await asyncio.to_thread(
        mark_creative_generation_delivered,
        actor=actor,
        receipt_id=receipt.id,
    )
    if str(getattr(job, "kind", "") or "") == "image":
        try:
            qa = await asyncio.to_thread(
                review_business_image_semantics_from_frozen_payload,
                provider_payload_json=receipt.provider_payload_json,
                job=job,
            )
        except (VisualCreativeError, TypeError, ValueError):
            qa = None
        warning = _semantic_qa_warning(qa)
        if warning:
            try:
                await target.answer(warning)
            except TelegramAPIError:
                # The image is already delivered and marked delivered. A secondary
                # advisory warning must never turn that success into an ambiguous
                # delivery or invite an automatic rerender.
                logger.debug(
                    "Could not deliver semantic QA advisory after image delivery",
                    exc_info=True,
                )
    await target.answer(
        "Можно сохранить результат из чата, создать ещё один или перейти к рекламе.",
        reply_markup=_result_rows(token, receipt),
    )
    return True


async def _finish_image(
    callback: CallbackQuery,
    *,
    actor,
    receipt: CreativeGenerationReceipt,
    job,
) -> bool:
    """Backward-compatible image completion boundary over the generic visual path."""

    return await _finish_visual(
        callback,
        actor=actor,
        receipt=receipt,
        job=job,
    )


async def _remember_job(actor, receipt: CreativeGenerationReceipt, job):
    return await asyncio.to_thread(
        remember_creative_generation_job,
        actor=actor,
        receipt_id=receipt.id,
        source_job_id=str(job.id),
        provider_status=str(job.status),
        provider_error_code=str(getattr(job, "error_code", "") or ""),
    )


async def _poll_existing(actor, receipt: CreativeGenerationReceipt):
    job = await asyncio.to_thread(
        poll_ad_visual,
        job_id=receipt.source_job_id,
        scope_id=actor.business_id,
    )
    current = await _remember_job(actor, receipt, job)
    return current, job


async def _submit_or_recover(actor, receipt: CreativeGenerationReceipt):
    current = await asyncio.to_thread(
        begin_creative_generation_submission,
        actor=actor,
        receipt_id=receipt.id,
    )
    if current.source_job_id:
        return await _poll_existing(actor, current)
    generator = (
        create_business_visual_from_frozen_payload
        if _receipt_kind(current) == "video"
        else create_business_image_from_frozen_payload
    )
    job = await asyncio.to_thread(
        generator,
        provider_payload_json=current.provider_payload_json,
        scope_id=actor.business_id,
        idempotency_key=current.idempotency_key,
    )
    saved = await _remember_job(actor, current, job)
    return saved, job


def _pending_rows(token: str, receipt: CreativeGenerationReceipt):
    return control._keyboard(
        [
            [("🔄 Проверить готовность", _receipt_callback("check", token, receipt))],
            [("🏠 Главная", f"cpj:home:{token}")],
        ]
    )


def _ambiguous_submission_rows(
    token: str,
    receipt: CreativeGenerationReceipt,
):
    return control._keyboard(
        [
            [("🔄 Проверить ещё раз", _receipt_callback("check", token, receipt))],
            [
                (
                    "⚠️ Завершить неопределённый запрос",
                    _receipt_callback("resolve", token, receipt),
                )
            ],
            [("🏠 Главная", f"cpj:home:{token}")],
        ]
    )


def _delivery_recovery_rows(
    token: str,
    receipt: CreativeGenerationReceipt,
    *,
    ambiguous: bool = False,
):
    rows: list[list[tuple[str, str]]] = []
    if ambiguous or receipt.delivery_claimed_at:
        rows.append(
            [(
                "📤 Отправить ещё раз (возможен дубль)",
                _receipt_callback("redeliver", token, receipt),
            )]
        )
    else:
        rows.append(
            [("🔄 Проверить файл ещё раз", _receipt_callback("check", token, receipt))]
        )
    rows.append(
        [(
            "🗑 Завершить и создать новую",
            _receipt_callback("abandon", token, receipt),
        )]
    )
    rows.append([("🏠 Главная", f"cpj:home:{token}")])
    return control._keyboard(rows)


async def _continue_generation(
    callback: CallbackQuery,
    *,
    actor,
    receipt: CreativeGenerationReceipt,
    auto_wait: bool = False,
) -> None:
    token = control._uuid_token(actor.business_id)
    if receipt.delivery_claimed_at:
        await control._callback_message(callback).answer(
            "⚠️ Отправка этого визуала могла уже пройти. Автоматический повтор "
            "заблокирован, чтобы не прислать дубль.",
            reply_markup=_delivery_recovery_rows(token, receipt, ambiguous=True),
        )
        return
    try:
        if receipt.status in {
            CreativeGenerationReceiptStatus.PREPARED,
            CreativeGenerationReceiptStatus.SUBMITTING,
        }:
            current, job = await _submit_or_recover(actor, receipt)
        else:
            current, job = await _poll_existing(actor, receipt)
    except VisualCreativeError:
        await control._callback_message(callback).answer(
            "Не удалось получить подтверждение от генератора. Запрос сохранён: "
            "следующая проверка продолжит тот же idempotent job и не создаст новый.",
            reply_markup=_pending_rows(token, receipt),
        )
        return
    except (LookupError, ValueError):
        await control._callback_message(callback).answer(
            "Состояние генерации изменилось. Откройте «Картинки и креативы» заново."
        )
        return

    if (
        auto_wait
        and current.status
        in {
            CreativeGenerationReceiptStatus.QUEUED,
            CreativeGenerationReceiptStatus.RUNNING,
        }
        and str(getattr(job, "status", "") or "") in {"queued", "running"}
    ):
        try:
            waited_job = await asyncio.to_thread(
                wait_ad_visual,
                job,
                wait_seconds=60,
            )
            current = await _remember_job(actor, current, waited_job)
            job = waited_job
        except VisualCreativeError as exc:
            # The durable receipt/source job remains authoritative. A transient
            # poll failure must not create another paid job; the recovery button
            # below can continue the same idempotent generation.
            logger.info("visual auto-wait deferred to explicit recovery: %s", exc)

    if (
        str(getattr(job, "status", "") or "") == "failed"
        and str(getattr(job, "error_code", "") or "")
        == "visual_gateway_submit_ambiguous"
    ):
        await control._callback_message(callback).answer(
            "⚠️ Не удалось достоверно установить, успел ли внешний генератор принять "
            "платный запрос до технического сбоя. ClientPlatform не запускает новый "
            "платный job автоматически. Можно проверить этот же запрос ещё раз или "
            "явно завершить его и затем подготовить новый; прежний запрос при этом "
            "мог уже быть принят провайдером.",
            reply_markup=_ambiguous_submission_rows(token, current),
        )
        return

    if await _retire_unavailable_completed_receipt(actor, current, job=job):
        await control._callback_message(callback).answer(
            "Готовый файл прошлой генерации уже недоступен во временном хранилище. "
            "Старый результат завершён — можно сразу создать новую картинку или видео.",
            reply_markup=_result_rows(token),
        )
        return

    if current.status == CreativeGenerationReceiptStatus.FAILED:
        await control._callback_message(callback).answer(
            visual_failure_message(job),
            reply_markup=_result_rows(token),
        )
        return
    finish = _finish_visual if _receipt_kind(current) == "video" else _finish_image
    if await finish(callback, actor=actor, receipt=current, job=job):
        return
    if current.status == CreativeGenerationReceiptStatus.SUCCEEDED:
        await control._callback_message(callback).answer(
            "Генератор завершил задачу, но готовый файл пока недоступен. "
            "Можно проверить ещё раз или завершить этот результат и создать новый.",
            reply_markup=_delivery_recovery_rows(token, current),
        )
        return
    await control._callback_message(callback).answer(
        "⏳ Визуал ещё создаётся. Повторно платная генерация не запускается.",
        reply_markup=_pending_rows(token, current),
    )


@router.callback_query(F.data.startswith("cpc:generate:"))
async def generate_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это подтверждение устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание визуалов недоступно для Вашей роли", show_alert=True
        )
        return
    try:
        receipt = await _receipt_for_callback(actor, receipt_token)
    except (LookupError, TypeError, ValueError):
        await callback.answer(
            "Это подтверждение относится к другому или уже устаревшему запросу",
            show_alert=True,
        )
        return
    await callback.answer()
    noun = "видео" if _receipt_kind(receipt) == "video" else "картинку"
    await _replace_or_answer(
        control._callback_message(callback),
        (
            f"⏳ Начинаю создавать {noun}. Если генерация завершится в ближайшую "
            "минуту, сразу отправлю файл сюда. Если потребуется больше времени, "
            "покажу кнопку проверки готовности — повторный платный job не запускается."
        ),
        reply_markup=None,
    )
    await _continue_generation(
        callback,
        actor=actor,
        receipt=receipt,
        auto_wait=True,
    )


@router.callback_query(F.data.startswith("cpc:abandon:"))
async def abandon_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это действие устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        abandoned = await asyncio.to_thread(
            abandon_creative_generation,
            actor=actor,
            receipt_id=receipt.id,
        )
    except TenantPermissionDenied:
        await callback.answer("Создание визуалов недоступно для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return
    if not abandoned:
        await callback.answer("Состояние результата уже изменилось", show_alert=True)
        return
    await callback.answer()
    result_text = (
        "Результат завершён. Теперь можно создать новое видео."
        if _receipt_kind(receipt) == "video"
        else "Результат завершён. Теперь можно создать новую картинку."
    )
    await control._callback_message(callback).answer(
        result_text,
        reply_markup=_result_rows(token),
    )


@router.callback_query(F.data.startswith("cpc:resolve:"))
async def resolve_ambiguous_creative_generation(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это действие устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        resolved = await asyncio.to_thread(
            abandon_ambiguous_creative_generation,
            actor=actor,
            receipt_id=receipt.id,
        )
    except TenantPermissionDenied:
        await callback.answer(
            "Создание визуалов недоступно для Вашей роли",
            show_alert=True,
        )
        return
    except VisualCreativeError:
        await callback.answer(
            "Не удалось подтвердить состояние у генератора. Проверьте запрос ещё раз.",
            show_alert=True,
        )
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer(
            "Не удалось подтвердить неопределённое состояние. Проверьте запрос ещё раз.",
            show_alert=True,
        )
        return
    if not resolved:
        await callback.answer(
            "Состояние уже изменилось. Проверьте генерацию ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        "Неопределённый запрос завершён вручную. Внешний провайдер мог успеть "
        "принять прежнюю генерацию до сбоя, поэтому предыдущий расход или результат "
        "нельзя полностью исключить. Новый запрос начнётся только после отдельного "
        "подтверждения.",
        reply_markup=_result_rows(token),
    )


@router.callback_query(F.data.startswith("cpc:redeliver:"))
async def redeliver_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это действие устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        authorized = await asyncio.to_thread(
            authorize_creative_generation_redelivery,
            actor=actor,
            receipt_id=receipt.id,
        )
    except TenantPermissionDenied:
        await callback.answer("Создание визуалов недоступно для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return
    if not authorized:
        await callback.answer(
            "Повторная отправка уже запущена или больше не нужна",
            show_alert=True,
        )
        return
    try:
        current = await _receipt_for_callback(actor, receipt_token)
    except LookupError:
        await callback.answer("Этот результат уже завершён", show_alert=True)
        return
    await callback.answer("Повторяю отправку по Вашему запросу…")
    await _continue_generation(callback, actor=actor, receipt=current)


@router.callback_query(F.data.startswith("cpc:accept:"))
async def accept_creative_result(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        await _receipt_for_callback(actor, receipt_token)
    except TenantPermissionDenied:
        await callback.answer("Этот результат недоступен для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return
    await callback.answer("Хорошо — оставляем этот вариант")


@router.callback_query(F.data.startswith("cpc:restyle:"))
async def restyle_creative_result(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        frozen_style = frozen_business_visual_style(receipt.provider_payload_json)
        style = resolve_visual_style_intent(
            request=receipt.request_text,
            selected=frozen_style,
        )
        inference = infer_visual_style_intent(receipt.request_text)
    except TenantPermissionDenied:
        await callback.answer("Этот результат недоступен для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return

    await state.set_data(
        {
            "creative_business_id": actor.business_id,
            "creative_business_token": token,
            "creative_kind": _receipt_kind(receipt),
            "creative_pending_prompt": receipt.request_text,
            "creative_brand_context": receipt.brand_context,
            "creative_country_code": receipt.country_code,
            "creative_style_intent": style.to_mapping(),
            "creative_saved_style_applied": False,
            "creative_style_inferred_fields": list(inference.explicit_fields),
        }
    )
    await state.set_state(ClientPlatformCreativeStudioState.choosing_style)
    await callback.answer("Меняем только визуальный стиль")
    await _show_style_dashboard(control._callback_message(callback), await state.get_data(), token)


@router.callback_query(F.data.startswith("cpc:variant:"))
async def create_another_visual_variant(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        new_receipt = await asyncio.to_thread(
            prepare_creative_generation,
            actor=actor,
            request_text=receipt.request_text,
            brand_context=receipt.brand_context,
            country_code=receipt.country_code,
            provider_payload_json=receipt.provider_payload_json,
        )
    except TenantPermissionDenied:
        await callback.answer("Создание визуалов недоступно для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Не удалось подготовить другой вариант", show_alert=True)
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        "🔄 Подготовил ещё один вариант с той же идеей и тем же стилем. "
        "Это будет отдельная платная генерация; она начнётся только после подтверждения.",
    )
    await _show_paid_generation_confirmation(
        control._callback_message(callback),
        token=token,
        receipt=new_receipt,
    )


@router.callback_query(F.data.startswith("cpc:download:"))
async def download_creative_file(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        if not receipt.source_job_id:
            raise LookupError("visual job is missing")
        job = await asyncio.to_thread(
            poll_ad_visual,
            job_id=receipt.source_job_id,
            scope_id=actor.business_id,
        )
        if str(getattr(job, "status", "") or "") != "succeeded" or not bool(
            getattr(job, "asset_ready", False)
        ):
            await callback.answer("Файл уже недоступен. Создайте визуал заново.", show_alert=True)
            return
        await callback.answer("Готовлю файл…")
        target = control._callback_message(callback)
        with tempfile.TemporaryDirectory(prefix="clientplatform-download-") as directory:
            path = await asyncio.to_thread(
                materialize_ad_visual,
                job,
                output_dir=directory,
                repair_blank_bands=False,
            )
            suffix = path.suffix.lower() or (
                ".mp4" if str(getattr(job, "kind", "") or "") == "video" else ".jpg"
            )
            filename = (
                "clientplatform-video" + suffix
                if str(getattr(job, "kind", "") or "") == "video"
                else "clientplatform-image" + suffix
            )
            await target.answer_document(
                FSInputFile(path, filename=filename),
                caption="📥 Файл для сохранения",
                reply_markup=_result_rows(token, receipt),
            )
    except TenantPermissionDenied:
        await callback.answer("Создание визуалов недоступно для Вашей роли", show_alert=True)
    except (LookupError, TypeError, ValueError):
        await callback.answer(
            "Файл уже недоступен. Создайте визуал заново.",
            show_alert=True,
        )
    except VisualCreativeError:
        await callback.answer(
            "Файл уже недоступен. Создайте визуал заново.",
            show_alert=True,
        )
    except TelegramAPIError:
        await callback.answer(
            "Telegram не смог отправить файл. Попробуйте ещё раз.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("cpc:check:"))
async def check_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта проверка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание визуалов недоступно для Вашей роли", show_alert=True
        )
        return
    try:
        receipt = await _receipt_for_callback(actor, receipt_token)
    except (LookupError, TypeError, ValueError):
        await callback.answer("Незавершённая генерация не найдена", show_alert=True)
        return
    await callback.answer("Проверяю готовность…")
    await _continue_generation(callback, actor=actor, receipt=receipt)


def _allowed(actor, check) -> bool:
    try:
        check()
    except TenantPermissionDenied:
        return False
    return True


def install_creative_studio_visibility(one_click: ModuleType) -> None:
    if bool(getattr(one_click, "_creative_studio_visibility_installed", False)):
        return
    original_more_rows = one_click._more_rows
    original_content_rows = one_click._content_tools_rows

    def more_rows(token: str, actor):
        rows = list(original_more_rows(token, actor))
        if _allowed(actor, actor.assert_can_manage_promotions):
            creative_row = [(nav.CREATIVES.label, f"cpc:open:{token}")]
            if creative_row not in rows:
                content_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if row and row[0][0] == nav.CONTENT_PROMOTION.label
                    ),
                    max(0, len(rows) - 2),
                )
                rows.insert(content_index, creative_row)
        return rows

    def content_tools_rows(token: str, actor):
        rows, help_lines = original_content_rows(token, actor)
        rows = [list(row) for row in rows]
        help_lines = list(help_lines)
        if _allowed(actor, actor.assert_can_manage_promotions):
            creative_button = (nav.CREATIVES.label, f"cpc:open:{token}")
            if not any(creative_button in row for row in rows):
                publication_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if row and str(row[0][1]).endswith(":publications")
                    ),
                    None,
                )
                if publication_index is None:
                    rows.insert(0, [creative_button])
                else:
                    rows[publication_index].insert(0, creative_button)
            help_line = (
                f"• создать картинку или видео для поста/рекламы → «{nav.CREATIVES.label}»"
            )
            if help_line not in help_lines:
                help_lines.insert(0, help_line)
        return rows, help_lines

    one_click._more_rows = more_rows
    one_click._content_tools_rows = content_tools_rows
    one_click._creative_studio_visibility_installed = True


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def install_creative_studio_safety(safety: ModuleType) -> None:
    if bool(getattr(safety, "_creative_studio_safety_installed", False)):
        return
    _extend_tuple(safety, "_CLIENTPLATFORM_CALLBACK_PREFIXES", "cpc:")
    _extend_tuple(
        safety,
        "_STATE_ESCAPE_PREFIXES",
        "cpc:open:",
        "cpc:new:",
        "cpc:video:",
        "cpc:editad:",
    )
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cpc:open:",
        "cpc:check:",
        "cpc:editad:",
        "cpc:st:open:",
        "cpc:st:p:",
        "cpc:st:d:",
        "cpc:st:s:",
        "cpc:st:reset:",
    )
    _extend_tuple(
        safety,
        "_ONE_SHOT_PREFIXES",
        "cpc:generate:",
        "cpc:abandon:",
        "cpc:redeliver:",
        "cpc:accept:",
        "cpc:restyle:",
        "cpc:variant:",
        "cpc:st:save:",
        "cpc:st:clear:",
        "cpc:st:go:",
    )

    original_state_local = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed", lambda _state, _data: False),
    )
    original_escape = cast(
        Callable[[str, str], bool],
        getattr(safety, "_callback_can_escape_state", lambda _state, _data: False),
    )

    def state_local_callback_allowed(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("ClientPlatformCreativeStudioState:choosing_style"):
            return callback_data.startswith(
                ("cpc:st:", "cpc:new:", "cpc:video:", "cpc:open:")
            )
        return original_state_local(current_state, callback_data)

    def callback_can_escape_state(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("ClientPlatformCreativeStudioState:"):
            if callback_data.startswith(
                ("cpc:open:", "cpc:new:", "cpc:video:", "cpj:home:")
            ):
                return True
        return original_escape(current_state, callback_data)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    setattr(safety, "_callback_can_escape_state", callback_can_escape_state)
    setattr(safety, "_creative_studio_safety_installed", True)


__all__ = [
    "ClientPlatformCreativeStudioState",
    "install_creative_studio_safety",
    "install_creative_studio_visibility",
    "router",
    "send_creative_studio_menu",
]