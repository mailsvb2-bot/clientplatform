from __future__ import annotations

from dataclasses import asdict, dataclass

from clientplatform.application.activity import get_business_profile, save_business_profile
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.tenancy import rename_business, resolve_tenant_context
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext
from services.db import atomic_db

_SCHEMA_VERSION = "2026-09-06.v1"


@dataclass(frozen=True, slots=True)
class CockpitSettingsSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    activity_description: str
    timezone_name: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_cockpit_settings(*, actor: TenantContext, business_name: str) -> CockpitSettingsSnapshot:
    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_manage_business()
    profile = get_business_profile(actor=current)
    return CockpitSettingsSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        activity_description=profile.activity_description,
        timezone_name=profile.timezone,
    )


def _resolve_actor(*, telegram_user_id: int, requested_business_id: str | None) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return actor, context.business_name


def resolve_cockpit_settings(
    *, telegram_user_id: int, requested_business_id: str | None = None
) -> CockpitSettingsSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_settings(actor=actor, business_name=business_name)


def update_cockpit_settings(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    business_name: str,
    activity_description: str,
    timezone_name: str,
) -> CockpitSettingsSnapshot:
    actor, _current_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_business()
    with atomic_db():
        business = rename_business(actor=actor, name=business_name)
        profile = save_business_profile(
            actor=actor,
            activity_description=activity_description,
            timezone_name=timezone_name,
        )
    return CockpitSettingsSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=actor.business_id,
        business_name=business.name,
        activity_description=profile.activity_description,
        timezone_name=profile.timezone,
    )


__all__ = [
    "CockpitSettingsSnapshot",
    "build_cockpit_settings",
    "resolve_cockpit_settings",
    "update_cockpit_settings",
]
