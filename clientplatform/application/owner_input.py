from __future__ import annotations

import re
from typing import Mapping

from clientplatform.domain.owner_input import OwnerInputResolution, OwnerInputSession
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.owner_input_repository import OwnerInputRepository
from services.db import get_db, get_db_ro


def get_owner_input_session(
    *, user_id: int, platform: str, surface: str = "official"
) -> OwnerInputSession | None:
    with get_db_ro() as conn:
        return OwnerInputRepository(conn).get(
            user_id=user_id, platform=platform, surface=surface
        )


def begin_owner_input(
    *,
    actor: TenantContext,
    platform: str,
    action: str,
    context: Mapping[str, object] | None = None,
    surface: str = "official",
) -> OwnerInputSession:
    with get_db() as conn:
        return OwnerInputRepository(conn).set(
            user_id=actor.user_id,
            platform=platform,
            business_id=actor.business_id,
            action=action,
            context=context,
            surface=surface,
        )


def clear_owner_input(
    *, user_id: int, platform: str, surface: str = "official"
) -> None:
    with get_db() as conn:
        OwnerInputRepository(conn).clear(
            user_id=user_id, platform=platform, surface=surface
        )


def resolve_owner_input(session: OwnerInputSession, value: object) -> OwnerInputResolution:
    raw_text = str(value or "").strip()
    if not raw_text:
        raise ValueError("owner input is empty")
    compact = " ".join(raw_text.split())

    if session.action == "activity_description":
        if not 3 <= len(raw_text) <= 2000:
            raise ValueError("activity description length is invalid")
        return OwnerInputResolution("activity-edit-text", (raw_text,))

    if session.action == "program_title":
        if len(compact) > 200:
            raise ValueError("program title is too long")
        return OwnerInputResolution("program-create-text", (compact,))

    if session.action in {"publication_draft", "offering", "program_lesson"}:
        parts = [part.strip() for part in raw_text.split("|", 1)]
        if len(parts) != 2 or not all(parts):
            raise ValueError("two text fields separated by | are required")
        first, second = parts
        if len(first) > 200:
            raise ValueError("title is too long")
        if session.action == "publication_draft":
            if len(second) > 4000:
                raise ValueError("publication body is too long")
            return OwnerInputResolution(
                "publication-new-text",
                (session.context["channel"], first, second),
            )
        if session.action == "offering":
            if len(second) > 1000:
                raise ValueError("offering description is too long")
            return OwnerInputResolution(
                "offering-new-text",
                (session.context["connector_key"], first, second),
            )
        if len(second) > 2048:
            raise ValueError("lesson material is too long")
        return OwnerInputResolution(
            "program-lesson-text",
            (
                session.context["program_id"],
                session.context["content_kind"],
                first,
                second,
            ),
        )

    if session.action == "booking_time":
        match = re.fullmatch(
            r"([0-3][0-9]\.[01][0-9]\.[0-9]{4}\s+[0-2][0-9]:[0-5][0-9])(?:\s+([1-9][0-9]{0,2}))?",
            compact,
        )
        if match is None:
            raise ValueError("booking time format is invalid")
        return OwnerInputResolution(
            "booking-open-text",
            (session.context["offering_id"], match.group(1), match.group(2) or "60"),
        )

    if session.action == "price":
        match = re.fullmatch(r"([0-9]+(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})", compact)
        if match is None:
            raise ValueError("price format is invalid")
        return OwnerInputResolution(
            "price-set-text",
            (session.context["offering_id"], match.group(1), match.group(2).upper()),
        )

    if session.action == "payment":
        legacy = re.fullmatch(
            r"оплата\s+([0-9]+(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})"
            r"(?:\s+([0-9a-f-]{6,36}|-))?(?:\s+([0-9a-f-]{6,36}|-))?"
            r"(?:\s*\|\s*(.{0,500}))?",
            raw_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if legacy is not None:
            return OwnerInputResolution(
                "payment-new-text",
                (
                    legacy.group(1),
                    legacy.group(2).upper(),
                    legacy.group(3) or "-",
                    legacy.group(4) or "-",
                    (legacy.group(5) or "").strip(),
                ),
            )
        match = re.fullmatch(
            r"([0-9]+(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})(?:\s*\|\s*(.{0,500}))?",
            raw_text,
            flags=re.DOTALL,
        )
        if match is None:
            raise ValueError("payment format is invalid")
        return OwnerInputResolution(
            "payment-new-text",
            (match.group(1), match.group(2).upper(), "-", "-", (match.group(3) or "").strip()),
        )

    if session.action == "member_user":
        if not compact.isdigit() or len(compact) > 20:
            raise ValueError("member account id is invalid")
        return OwnerInputResolution("member-add-text", (compact, session.context["role_code"]))

    raise ValueError("owner input action is unsupported")
