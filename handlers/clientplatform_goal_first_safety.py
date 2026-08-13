from __future__ import annotations

"""Canonical goal-first owner action plus its interaction-safety wiring."""

import asyncio
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, cast


@dataclass(frozen=True, slots=True)
class OwnerNavigationAction:
    """One owner-facing action shared by UI, recovery and callback routing."""

    label: str
    callback_prefix: str

    def callback(self, business_token: str) -> str:
        return f"{self.callback_prefix}{business_token}"

    def recovery(self, context: str, *, continuation: str) -> str:
        return f"{context} Нажмите «{self.label}» {continuation}"


ACQUIRE_CLIENTS = OwnerNavigationAction(
    label="🚀 Найти новых клиентов",
    callback_prefix="cpo:start:",
)


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def _install_managed_campaign_goal_first() -> None:
    one_click = sys.modules.get("handlers.clientplatform_one_click_experience")
    goal_first = sys.modules.get("handlers.clientplatform_goal_first_autopilot")
    control = sys.modules.get("handlers.clientplatform_control")
    if one_click is None or goal_first is None or control is None:
        return
    if bool(getattr(one_click, "_managed_campaign_goal_first_installed", False)):
        return

    # Preserve the historical composition hook name, but point it at the
    # canonical tenant-owned managed campaign implementation.
    setattr(
        one_click,
        "create_ad_publication_draft",
        one_click.create_managed_ad_publication_draft,
    )

    async def prepare_goal_result(event, state, *, data: dict, region_ids: tuple[int, ...]) -> None:
        actor = await control._actor(one_click._user_id(event), str(data["business_id"]))
        try:
            promotion = await asyncio.to_thread(
                one_click.create_slot_promotion,
                actor=actor,
                slot_id=str(data["slot_id"]),
                channel=one_click.PromotionChannel.WEBSITE,
            )
        except (one_click.PromotionError, one_click.TenantPermissionDenied):
            await one_click._draft_failure(
                event,
                state,
                business_token=str(data["business_token"]),
            )
            return

        try:
            source_url = one_click._link(
                await one_click._username(event),
                promotion.campaign.source_token,
            )
        except (RuntimeError, ValueError):
            await one_click._draft_failure(
                event,
                state,
                business_token=str(data["business_token"]),
            )
            return

        try:
            draft = await asyncio.to_thread(
                one_click.create_managed_ad_publication_draft,
                actor=actor,
                promotion_campaign_id=promotion.campaign.id,
                connection_id=str(data["connection_id"]),
                region_ids=region_ids,
                source_url=source_url,
            )
        except (
            one_click.AdConnectionError,
            one_click.TenantPermissionDenied,
            one_click.YandexDirectError,
        ):
            await one_click._draft_failure(
                event,
                state,
                business_token=str(data["business_token"]),
            )
            return

        external_campaign_id = str(
            getattr(draft.job, "external_campaign_id", "")
            or data.get("external_campaign_id")
            or ""
        )
        next_data = {
            **data,
            "promotion_campaign_id": promotion.campaign.id,
            "external_campaign_id": external_campaign_id,
            "external_campaign_name": draft.campaign_name,
            "source_url": source_url,
            "job_id": draft.job.id,
            "creative_title": draft.job.title,
            "creative_body": draft.job.text,
            "creative_job_id": "",
        }
        await state.set_state(goal_first.GoalFirstAutopilotState.ready)
        await state.set_data(next_data)
        await one_click._target(event).answer(
            "✅ Реклама подготовлена — всё готово\n\n"
            "ClientPlatform сама выбрала свободное время и выделила этому продвижению "
            "отдельную кампанию Яндекса. Кампания остаётся выключенной.\n\n"
            f"{draft.job.title}\n\n{draft.job.text}\n\n"
            "Можно настроить текст или медиа, либо перейти к отдельной проверке запуска.",
            reply_markup=goal_first._result_keyboard(
                str(data["business_token"]),
                next_data,
            ),
        )

    async def choose_goal_region(
        callback,
        state,
        *,
        data: dict,
        campaign_id: str = "",
        campaign_name: str = "",
    ) -> None:
        del campaign_id, campaign_name
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        try:
            jobs = await asyncio.to_thread(one_click.list_ad_publications, actor=actor)
        except (one_click.AdConnectionError, one_click.TenantPermissionDenied):
            jobs = []
        saved = one_click._recent(
            jobs,
            connection_id=str(data["connection_id"]),
        )
        regions = tuple(getattr(saved, "region_ids", ()) or ()) if saved else ()
        if regions:
            await one_click._prepare_draft(
                callback,
                state,
                data=data,
                region_ids=regions,
            )
            return

        await state.set_state(one_click.OneClickOwnerState.waiting_region)
        await state.set_data(data)
        await control._callback_message(callback).answer(
            "Осталось только указать регион: где искать клиентов? Это нужно спросить "
            "только в первый раз — дальше ClientPlatform запомнит выбор, а рекламную "
            "кампанию создаст и привяжет сама.",
            reply_markup=control._keyboard(
                [
                    [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                    [("Санкт-Петербург", "cpo:region:2")],
                    [("Другой регион", "cpo:region:other")],
                    [("🏠 Не сейчас", f"cpj:home:{data['business_token']}")],
                ]
            ),
        )

    setattr(goal_first, "_prepare_goal_result", prepare_goal_result)
    setattr(goal_first, "_choose_goal_region", choose_goal_region)
    setattr(one_click, "_prepare_draft", prepare_goal_result)
    if hasattr(one_click, "_choose_campaign"):
        delattr(one_click, "_choose_campaign")
    setattr(one_click, "_managed_campaign_goal_first_installed", True)


def install_goal_first_safety(safety: ModuleType) -> None:
    if bool(getattr(safety, "_goal_first_safety_installed", False)):
        return

    _extend_tuple(safety, "_SENSITIVE_STATE_PREFIXES", "GoalFirstAutopilotState:")
    _extend_tuple(
        safety,
        "_ONE_SHOT_PREFIXES",
        "cpo:gen:",
        "cpo:launch:",
        "cpo:launch-confirm:",
        "cpo:custom-clear:",
    )
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cpo:custom:",
        "cpo:custom-text:",
        "cpo:custom-image:",
        "cpo:custom-video:",
        "cpo:custom-done:",
        "cpo:genask:",
        "cpo:gencheck:",
    )

    original_state_local = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed"),
    )
    original_escape = cast(
        Callable[[str, str], bool],
        getattr(safety, "_callback_can_escape_state"),
    )

    def state_local_callback_allowed(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("GoalFirstAutopilotState:ready"):
            return callback_data.startswith(("cpo:custom:", "cpo:launch:"))
        if current_state.startswith("GoalFirstAutopilotState:customizing"):
            return callback_data.startswith(
                (
                    "cpo:custom:",
                    "cpo:custom-text:",
                    "cpo:custom-image:",
                    "cpo:custom-video:",
                    "cpo:custom-clear:",
                    "cpo:custom-done:",
                    "cpo:genask:",
                    "cpo:ads:",
                    "cpo:launch:",
                )
            )
        if current_state.startswith("GoalFirstAutopilotState:confirming_generation"):
            return callback_data.startswith(("cpo:gen:", "cpo:custom:"))
        if current_state.startswith("GoalFirstAutopilotState:generation_pending"):
            return callback_data.startswith("cpo:gencheck:")
        if current_state.startswith("GoalFirstAutopilotState:confirming_launch"):
            return callback_data.startswith("cpo:launch-confirm:")
        return original_state_local(current_state, callback_data)

    def callback_can_escape_state(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("GoalFirstAutopilotState:"):
            if callback_data.startswith(("cpj:home:", ACQUIRE_CLIENTS.callback_prefix)):
                return True
        return original_escape(current_state, callback_data)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    setattr(safety, "_callback_can_escape_state", callback_can_escape_state)
    _install_managed_campaign_goal_first()
    setattr(safety, "_goal_first_safety_installed", True)


__all__ = [
    "ACQUIRE_CLIENTS",
    "OwnerNavigationAction",
    "install_goal_first_safety",
]