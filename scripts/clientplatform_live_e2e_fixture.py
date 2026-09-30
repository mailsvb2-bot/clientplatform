"""Provision a deterministic, synthetic ClientPlatform staging fixture.

This is intentionally a CLI-only staging tool. It uses canonical application
services and never exposes a runtime HTTP/test backdoor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

E2E_PREFIX_ROOT = "[ClientPlatform E2E"
_RUN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_FIXED_BOOKING_LOCAL_START = "15.01.2099 12:00"
_FIXED_BOOKING_START_UTC = "2099-01-15T12:00:00+00:00"
_FIXED_BOOKING_DURATION_MINUTES = 45
_FIXED_PRICE_MINOR = 123400


class FixtureSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixtureConfig:
    namespace: str
    run_key: str
    owner_user_id: int
    member_user_id: int
    customer_subjects: dict[str, str]

    @property
    def prefix(self) -> str:
        return f"[ClientPlatform E2E:{self.namespace}]"

    @property
    def primary_name(self) -> str:
        return f"{self.prefix} {self.run_key}:primary"

    @property
    def secondary_name(self) -> str:
        return f"{self.prefix} {self.run_key}:secondary"


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise FixtureSafetyError(f"missing_environment:{name}")
    return value


def _positive_int(env: Mapping[str, str], name: str) -> int:
    raw = _required(env, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise FixtureSafetyError(f"invalid_integer:{name}") from exc
    if value <= 0:
        raise FixtureSafetyError(f"positive_integer_required:{name}")
    return value


def validate_fixture_environment(env: Mapping[str, str]) -> FixtureConfig:
    if str(env.get("APP_ENV") or "").strip().casefold() != "staging":
        raise FixtureSafetyError("APP_ENV_must_be_staging")
    if not _truthy(env.get("CLIENTPLATFORM_LIVE_E2E")):
        raise FixtureSafetyError("CLIENTPLATFORM_LIVE_E2E_must_be_1")
    if not _truthy(env.get("CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS")):
        raise FixtureSafetyError("dedicated_test_accounts_not_confirmed")
    if _truthy(env.get("CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS")):
        raise FixtureSafetyError("production_credentials_forbidden")
    if _truthy(env.get("CLIENTPLATFORM_LIVE_E2E_REAL_MONEY")):
        raise FixtureSafetyError("real_money_forbidden")
    if (
        str(env.get("CLIENTPLATFORM_E2E_FIXTURE_DATABASE_ATTESTATION") or "")
        .strip()
        .casefold()
        != "synthetic-staging"
    ):
        raise FixtureSafetyError("synthetic_staging_database_attestation_required")

    namespace = _required(env, "CLIENTPLATFORM_E2E_FIXTURE_NAMESPACE").casefold()
    run_key = _required(env, "CLIENTPLATFORM_E2E_FIXTURE_RUN_KEY").casefold()
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise FixtureSafetyError("fixture_namespace_invalid")
    if not _RUN_KEY_RE.fullmatch(run_key):
        raise FixtureSafetyError("fixture_run_key_invalid")

    owner_user_id = _positive_int(env, "CLIENTPLATFORM_E2E_FIXTURE_OWNER_USER_ID")
    member_user_id = _positive_int(env, "CLIENTPLATFORM_E2E_FIXTURE_MEMBER_USER_ID")
    if owner_user_id == member_user_id:
        raise FixtureSafetyError("fixture_owner_and_member_must_differ")

    subjects = {
        platform: _required(
            env,
            f"CLIENTPLATFORM_E2E_FIXTURE_CUSTOMER_{platform.upper()}_SUBJECT",
        )
        for platform in ("telegram", "vk", "max")
    }
    return FixtureConfig(
        namespace=namespace,
        run_key=run_key,
        owner_user_id=owner_user_id,
        member_user_id=member_user_id,
        customer_subjects=subjects,
    )


def _plan() -> dict[str, object]:
    return {
        "mode": "staging-cli-only",
        "production_forbidden": True,
        "real_money_forbidden": True,
        "delete_based_reset_forbidden": True,
        "fixture_objects": [
            "two tenant businesses",
            "owner/member RBAC",
            "business profile and capabilities",
            "primary/secondary offering",
            "customer with Telegram/VK/MAX identities",
            "program and text lesson",
            "booking slot",
            "publication draft",
            "offering price",
            "manual sandbox payment fact",
        ],
        "reset": (
            "archive only stale active businesses owned by the dedicated fixture "
            "owner whose names start with the exact namespace E2E prefix"
        ),
    }


def _ensure_business(*, owner_user_id: int, name: str):
    from clientplatform.application.tenancy import (
        create_business,
        list_accessible_businesses,
    )

    exact = [
        access
        for access in list_accessible_businesses(user_id=owner_user_id)
        if access.business.name == name
    ]
    if len(exact) > 1:
        raise FixtureSafetyError("duplicate_fixture_business_name")
    if exact:
        access = exact[0]
        if access.membership.role.value != "owner":
            raise FixtureSafetyError("fixture_business_not_owned_by_fixture_owner")
        return access
    return create_business(owner_user_id=owner_user_id, name=name)


def _archive_stale_businesses(config: FixtureConfig) -> list[str]:
    from clientplatform.application.tenancy import (
        archive_business,
        list_accessible_businesses,
        resolve_tenant_context,
    )

    keep = {config.primary_name, config.secondary_name}
    archived: list[str] = []
    for access in list_accessible_businesses(user_id=config.owner_user_id):
        business = access.business
        if business.name in keep:
            continue
        if not business.name.startswith(config.prefix + " "):
            continue
        if access.membership.role.value != "owner":
            continue
        actor = resolve_tenant_context(
            user_id=config.owner_user_id,
            business_id=business.id,
        )
        archive_business(actor=actor)
        archived.append(business.id)
    return archived


def _ensure_customer(*, actor, config: FixtureConfig):
    from clientplatform.application.customers import (
        attach_customer_identity,
        create_customer,
        find_customer_by_identity,
    )
    from clientplatform.domain.customers import CustomerNotFound

    telegram_subject = config.customer_subjects["telegram"]
    try:
        record = find_customer_by_identity(
            actor=actor,
            platform="telegram",
            external_subject=telegram_subject,
        )
        customer = record.customer
    except CustomerNotFound:
        customer = create_customer(
            actor=actor,
            display_name=f"E2E Customer {config.run_key}",
        )

    for platform, subject in config.customer_subjects.items():
        attach_customer_identity(
            actor=actor,
            customer_id=customer.id,
            platform=platform,
            external_subject=subject,
            display_name=f"E2E Customer {config.run_key}",
        )
    return customer


def _ensure_booking_slot(*, actor, offering_id: str):
    from clientplatform.application.bookings import (
        create_booking_slot,
        list_booking_slots,
    )

    for view in list_booking_slots(
        actor=actor,
        offering_id=offering_id,
        include_unavailable=True,
    ):
        slot = view.slot
        if (
            slot.offering_id == offering_id
            and slot.starts_at == _FIXED_BOOKING_START_UTC
            and slot.duration_minutes == _FIXED_BOOKING_DURATION_MINUTES
            and slot.status.value in {"open", "booked"}
        ):
            return view
    return create_booking_slot(
        actor=actor,
        offering_id=offering_id,
        local_start=_FIXED_BOOKING_LOCAL_START,
        duration_minutes=_FIXED_BOOKING_DURATION_MINUTES,
    )


def _seed(config: FixtureConfig, *, retire_stale: bool) -> dict[str, object]:
    from clientplatform.application import admin_ops
    from clientplatform.application.activity import (
        complete_business_profile,
        create_business_offering,
        enable_business_capability,
        save_business_profile,
    )
    from clientplatform.application.programs import (
        add_program_lesson,
        create_program,
        publish_program,
    )
    from clientplatform.application.tenancy import (
        grant_business_member,
        resolve_tenant_context,
        set_owner_control_workspace,
    )
    from services.store import store

    store.ensure_user(
        config.owner_user_id,
        username=f"cp_e2e_owner_{config.namespace}",
        first_name="ClientPlatform E2E Owner",
    )
    store.ensure_user(
        config.member_user_id,
        username=f"cp_e2e_member_{config.namespace}",
        first_name="ClientPlatform E2E Member",
    )

    archived = _archive_stale_businesses(config) if retire_stale else []
    primary_access = _ensure_business(
        owner_user_id=config.owner_user_id,
        name=config.primary_name,
    )
    secondary_access = _ensure_business(
        owner_user_id=config.owner_user_id,
        name=config.secondary_name,
    )
    primary = resolve_tenant_context(
        user_id=config.owner_user_id,
        business_id=primary_access.business.id,
    )
    secondary = resolve_tenant_context(
        user_id=config.owner_user_id,
        business_id=secondary_access.business.id,
    )

    member = grant_business_member(
        actor=primary,
        user_id=config.member_user_id,
        role="manager",
    )
    for platform in ("telegram", "vk", "max"):
        set_owner_control_workspace(
            user_id=config.owner_user_id,
            platform=platform,
            business_id=primary.business_id,
        )

    for actor, label in ((primary, "primary"), (secondary, "secondary")):
        save_business_profile(
            actor=actor,
            activity_description=f"Synthetic {label} ClientPlatform live E2E business",
            timezone_name="UTC",
        )
        enable_business_capability(actor=actor, connector_key="consultations")
        enable_business_capability(actor=actor, connector_key="programs")
        complete_business_profile(actor=actor)

    primary_capability = enable_business_capability(
        actor=primary,
        connector_key="consultations",
    )
    secondary_capability = enable_business_capability(
        actor=secondary,
        connector_key="consultations",
    )
    primary_offering = create_business_offering(
        actor=primary,
        capability_id=primary_capability.id,
        title="E2E consultation",
        description="Synthetic offering for protected live E2E",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:offering",
    )
    secondary_offering = create_business_offering(
        actor=secondary,
        capability_id=secondary_capability.id,
        title="E2E isolation consultation",
        description="Synthetic cross-tenant denial target",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:secondary-offering",
    )

    customer = _ensure_customer(actor=primary, config=config)

    program = create_program(
        actor=primary,
        title="E2E program",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:program",
    )
    lesson = add_program_lesson(
        actor=primary,
        program_id=program.id,
        title="E2E lesson",
        content_kind="text",
        content_ref="Synthetic ClientPlatform E2E lesson content.",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:lesson",
    )
    program = publish_program(actor=primary, program_id=program.id)

    booking = _ensure_booking_slot(
        actor=primary,
        offering_id=primary_offering.id,
    )
    publication = admin_ops.create_publication_draft(
        actor=primary,
        title="E2E publication",
        body="Synthetic ClientPlatform live E2E publication draft.",
        channel="telegram",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:publication",
    )
    price = admin_ops.set_offering_price(
        actor=primary,
        offering_id=primary_offering.id,
        amount_minor=_FIXED_PRICE_MINOR,
        currency="RUB",
    )
    payment = admin_ops.record_payment(
        actor=primary,
        amount_minor=_FIXED_PRICE_MINOR,
        currency="RUB",
        customer_id=customer.id,
        offering_id=primary_offering.id,
        note="Synthetic staging E2E payment fact; no provider charge.",
        provider="manual",
        idempotency_key=f"e2e:{config.namespace}:{config.run_key}:payment",
    )

    result: dict[str, object] = {
        "schema_version": 1,
        "namespace": config.namespace,
        "run_key": config.run_key,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archived_stale_business_ids": sorted(archived),
        "primary_business_id": primary.business_id,
        "secondary_business_id": secondary.business_id,
        "member_membership_id": member.id,
        "primary_capability_id": primary_capability.id,
        "primary_offering_id": primary_offering.id,
        "secondary_offering_id": secondary_offering.id,
        "customer_id": customer.id,
        "program_id": program.id,
        "lesson_id": lesson.id,
        "booking_slot_id": booking.slot.id,
        "publication_id": publication.id,
        "offering_price_id": price.id,
        "payment_id": payment.id,
    }
    return result


def _write_result(result: dict[str, object], output: str | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        path = Path(output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print("CLIENTPLATFORM_LIVE_E2E_FIXTURE_OK " + json.dumps(
        {
            "namespace": result["namespace"],
            "run_key": result["run_key"],
            "primary_business_id": result["primary_business_id"],
            "secondary_business_id": result["secondary_business_id"],
        },
        sort_keys=True,
    ))


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--seed", action="store_true")
    mode.add_argument("--reset", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.plan:
        print(
            "CLIENTPLATFORM_LIVE_E2E_FIXTURE_PLAN "
            + json.dumps(_plan(), sort_keys=True)
        )
        return 0

    config = validate_fixture_environment(os.environ)
    result = _seed(config, retire_stale=bool(args.reset))
    _write_result(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
