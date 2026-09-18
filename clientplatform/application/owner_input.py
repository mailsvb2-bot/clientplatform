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

    if session.action == "event_warmup_text":
        if not 1 <= len(raw_text) <= 3500:
            raise ValueError("event warmup text length is invalid")
        event_id = str(session.context.get("event_id") or "").strip()
        requested_days = str(session.context.get("requested_days") or "").strip()
        position = str(session.context.get("position") or "").strip()
        if not event_id or not requested_days.isdigit() or not position.isdigit():
            raise ValueError("event warmup edit context is invalid")
        return OwnerInputResolution(
            "event-warmup-edit-text",
            (event_id, requested_days, position, raw_text),
        )

    if session.action == "event_followup_text":
        if not 1 <= len(raw_text) <= 3500:
            raise ValueError("event followup text length is invalid")
        event_id = str(session.context.get("event_id") or "").strip()
        index = str(session.context.get("index") or "").strip()
        segment = str(session.context.get("segment") or "").strip()
        stage = str(session.context.get("stage") or "").strip()
        if not event_id or not index.isdigit() or not segment or not stage.isdigit():
            raise ValueError("event followup edit context is invalid")
        return OwnerInputResolution(
            "event-followup-edit-text",
            (event_id, index, segment, stage, raw_text),
        )

    if session.action == "event_warmup_days":
        event_id = str(session.context.get("event_id") or "").strip()
        maximum = str(session.context.get("maximum") or "").strip()
        if (
            not event_id
            or not maximum.isdigit()
            or not compact.isdigit()
            or not 0 <= int(compact) <= int(maximum)
        ):
            raise ValueError("event warmup days are invalid")
        return OwnerInputResolution(
            "event-warmup-days-text",
            (event_id, compact),
        )

    if session.action == "online_event":
        step = str(session.context.get("step") or "").strip().casefold()
        if step == "title":
            if not compact or len(compact) > 180:
                raise ValueError("event title is invalid")
            return OwnerInputResolution("event-wizard-title-text", (compact,))
        if step == "count":
            if not compact.isdigit() or not 1 <= int(compact) <= 31:
                raise ValueError("event session count is invalid")
            return OwnerInputResolution("event-wizard-count-text", (compact,))
        if step == "timezone":
            if not compact or len(compact) > 80:
                raise ValueError("event timezone is invalid")
            return OwnerInputResolution("event-wizard-timezone-text", (compact,))
        if step == "manual_window":
            if not compact or len(compact) > 80:
                raise ValueError("event session window is invalid")
            return OwnerInputResolution("event-wizard-window-text", (compact,))
        if step == "room":
            if not raw_text or len(raw_text) > 2048:
                raise ValueError("event room URL is invalid")
            return OwnerInputResolution("event-wizard-room-text", (raw_text,))
        if step == "warmup_days":
            if not compact.isdigit() or int(compact) > 366:
                raise ValueError("event warmup days are invalid")
            return OwnerInputResolution("event-wizard-warmup-text", (compact,))

        # Legacy durable sessions created before the multi-step wizard remain
        # resolvable so an in-flight owner interaction is never stranded.
        parts = [part.strip() for part in raw_text.split("|")]
        if len(parts) not in {2, 3, 4} or not all(parts[:2]):
            raise ValueError("event title and time are required")
        title, local_time = parts[:2]
        join_url = parts[2] if len(parts) >= 3 else ""
        offer_url = parts[3] if len(parts) == 4 else ""
        if len(title) > 180:
            raise ValueError("event title is too long")
        if join_url == "-":
            join_url = ""
        if offer_url == "-":
            offer_url = ""
        return OwnerInputResolution(
            "event-create-text",
            (title, local_time, join_url, offer_url),
        )

    if session.action == "event_join_url":
        join_url = str(raw_text or "").strip()
        if not join_url:
            raise ValueError("event join URL is required")
        return OwnerInputResolution(
            "event-join-text",
            (session.context["event_id"], join_url),
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
