from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_MAX_ITEM = 500
_MAX_LIST_ITEMS = 32
_MAX_LONG_TEXT = 2000


def _normalize_optional(value: object, *, maximum: int = _MAX_LONG_TEXT) -> str | None:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ValueError(f"business profile field must be at most {maximum} characters")
    return normalized


def _normalize_items(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        source = [values]
    elif isinstance(values, (list, tuple)):
        source = list(values)
    else:
        raise ValueError("business profile list field must be a list of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in source:
        item = _normalize_optional(value, maximum=_MAX_ITEM)
        if item is None:
            continue
        marker = item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        normalized.append(item)
        if len(normalized) > _MAX_LIST_ITEMS:
            raise ValueError(f"business profile list field must contain at most {_MAX_LIST_ITEMS} items")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class BusinessProfileDetails:
    """Durable structured facts explicitly supplied by a business owner.

    These fields complement the canonical BusinessProfile. They are deliberately
    provider-neutral and never contain inferred payment, legal or qualification
    claims. Empty values mean "not supplied", not "AI should guess".
    """

    services: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    prices: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()
    geo: tuple[str, ...] = ()
    working_hours: str | None = None
    contacts: tuple[str, ...] = ()
    booking_rules: str | None = None
    tone_of_voice: str | None = None
    allowed_claims: tuple[str, ...] = ()
    prohibited_claims: tuple[str, ...] = ()
    legal_constraints: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    visual_assets: tuple[str, ...] = ()
    faq: tuple[str, ...] = ()
    sales_terms: str | None = None
    preferred_conversion_action: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "services",
            "products",
            "prices",
            "audiences",
            "geo",
            "contacts",
            "allowed_claims",
            "prohibited_claims",
            "legal_constraints",
            "source_urls",
            "visual_assets",
            "faq",
        ):
            object.__setattr__(self, field_name, _normalize_items(getattr(self, field_name)))
        for field_name in (
            "working_hours",
            "booking_rules",
            "tone_of_voice",
            "sales_terms",
            "preferred_conversion_action",
        ):
            object.__setattr__(self, field_name, _normalize_optional(getattr(self, field_name)))

    def to_payload(self) -> dict[str, object]:
        return {
            "services": list(self.services),
            "products": list(self.products),
            "prices": list(self.prices),
            "audiences": list(self.audiences),
            "geo": list(self.geo),
            "working_hours": self.working_hours,
            "contacts": list(self.contacts),
            "booking_rules": self.booking_rules,
            "tone_of_voice": self.tone_of_voice,
            "allowed_claims": list(self.allowed_claims),
            "prohibited_claims": list(self.prohibited_claims),
            "legal_constraints": list(self.legal_constraints),
            "source_urls": list(self.source_urls),
            "visual_assets": list(self.visual_assets),
            "faq": list(self.faq),
            "sales_terms": self.sales_terms,
            "preferred_conversion_action": self.preferred_conversion_action,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "BusinessProfileDetails":
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise ValueError("business profile details must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("business profile details contain unsupported fields")
        return cls(**{key: payload.get(key) for key in allowed})


def business_profile_details_to_json(details: BusinessProfileDetails) -> str:
    if not isinstance(details, BusinessProfileDetails):
        raise ValueError("details must be BusinessProfileDetails")
    return json.dumps(details.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def business_profile_details_from_json(raw: object) -> BusinessProfileDetails:
    text = str(raw or "{}").strip() or "{}"
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("stored business profile details are invalid") from exc
    return BusinessProfileDetails.from_payload(payload)


def _labeled_items(text: str, *labels: str) -> tuple[str, ...]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)(?:^|[;.\n])\s*(?:{label_pattern})\s*:\s*([^;\n]+)",
        text,
    )
    if match is None:
        return ()
    return _normalize_items([part for part in re.split(r",|\|", match.group(1)) if part.strip()])


def _labeled_text(text: str, *labels: str) -> str | None:
    values = _labeled_items(text, *labels)
    return values[0] if values else None


def extract_explicit_business_profile_details(description: str) -> BusinessProfileDetails:
    """Extract only facts that are explicit in owner text.

    This deterministic baseline intentionally prefers missing data over invented
    data. AI extraction may later enrich suggestions, but durable state keeps the
    same confirmation boundary and schema.
    """

    text = str(description or "").replace("\x00", " ").strip()
    prices = _normalize_items(
        re.findall(
            r"(?<!\w)\d[\d\s]{0,8}(?:[.,]\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля)?|RUB|€|\$)",
            text,
            flags=re.IGNORECASE,
        )
    )
    emails = re.findall(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}(?![\w.-])", text)
    phones = re.findall(r"(?<!\d)(?:\+\d[\d ()-]{8,}\d)(?!\d)", text)
    handles = re.findall(r"(?<![\w@])@[A-Za-z0-9_]{4,32}\b", text)
    source_urls = _normalize_items(re.findall(r"https?://[^\s;,]+", text, flags=re.IGNORECASE))
    contacts = _normalize_items([*emails, *phones, *handles])

    return BusinessProfileDetails(
        services=_labeled_items(text, "услуги", "услуга", "services"),
        products=_labeled_items(text, "продукты", "продукт", "products"),
        prices=prices,
        audiences=_labeled_items(text, "аудитория", "клиенты", "для кого", "audience"),
        geo=_labeled_items(text, "география", "город", "регион", "geo"),
        working_hours=_labeled_text(text, "часы работы", "график", "working hours"),
        contacts=contacts,
        booking_rules=_labeled_text(text, "правила записи", "запись", "booking rules"),
        tone_of_voice=_labeled_text(text, "тон", "стиль общения", "tone of voice"),
        allowed_claims=_labeled_items(text, "можно утверждать", "разрешенные утверждения"),
        prohibited_claims=_labeled_items(text, "нельзя утверждать", "запрещенные утверждения"),
        legal_constraints=_labeled_items(text, "ограничения", "правовые ограничения", "compliance"),
        source_urls=source_urls,
        visual_assets=_labeled_items(text, "визуальные материалы", "brand assets"),
        faq=_labeled_items(text, "faq", "частые вопросы"),
        sales_terms=_labeled_text(text, "условия продажи", "возврат", "условия возврата"),
        preferred_conversion_action=_labeled_text(
            text,
            "главное действие клиента",
            "цель клиента",
            "conversion action",
        ),
    )


def business_profile_review_lines(details: BusinessProfileDetails) -> tuple[str, ...]:
    lines: list[str] = []
    for title, values in (
        ("Услуги", details.services),
        ("Продукты", details.products),
        ("Цены", details.prices),
        ("Для кого", details.audiences),
        ("География", details.geo),
        ("Контакты", details.contacts),
    ):
        if values:
            lines.append(f"{title}: {', '.join(values)}")
    for title, value in (
        ("Часы работы", details.working_hours),
        ("Правила записи", details.booking_rules),
        ("Стиль общения", details.tone_of_voice),
        ("Главное действие клиента", details.preferred_conversion_action),
    ):
        if value:
            lines.append(f"{title}: {value}")
    if details.source_urls:
        lines.append(f"Источники: {', '.join(details.source_urls)}")
    return tuple(lines)
