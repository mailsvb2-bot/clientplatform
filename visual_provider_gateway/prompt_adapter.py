from __future__ import annotations

"""Provider-specific prompt shaping that preserves ClientPlatform semantic intent.

The application compiler owns meaning. This adapter may only reshape that frozen
meaning for a provider's prompt contract; it must never invent a new subject,
offer, claim, audience or transformation.
"""

from dataclasses import replace

from .models import CreativeBrief


PROMPT_ADAPTER_VERSION = 2

_RUNWAY_PROMPT_LIMIT = 1000
_YANDEX_PROMPT_LIMIT = 500


def _lines(prompt: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in str(prompt or "").splitlines()
        if line.strip()
    )


def _bounded_join(parts: list[str], *, limit: int) -> str:
    result = ""
    for part in parts:
        token = " ".join(str(part or "").split()).strip()
        if not token:
            continue
        candidate = token if not result else result + "\n" + token
        if len(candidate) <= limit:
            result = candidate
            continue
        remaining = limit - len(result) - (1 if result else 0)
        if remaining > 80:
            clipped = token[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:.")
            result = clipped if not result else result + "\n" + clipped
        break
    return result.strip()


def _priority_lines(brief: CreativeBrief) -> list[str]:
    lines = list(_lines(brief.prompt))
    if not lines:
        return []
    selectors = (
        "owner request",
        "mandatory",
        "must",
        "listening",
        "watching",
        "reading",
        "using",
        "holds",
        "transformation",
        "initial state",
        "final state",
        "chronology",
        "visual style intent",
        "style choices",
        "color temperature",
        "emotional tone",
        "visual energy",
        "visual language",
        "lighting",
        "contrast",
        "visual detail",
        "composition",
        "motion",
        "commercial presentation",
        "copy space",
        "business grounding",
        "do not rely on readable text",
    )
    selected: list[str] = []
    for line in lines:
        folded = line.casefold()
        if any(marker in folded for marker in selectors):
            selected.append(line)
    if not selected:
        selected = lines[:5]
    # Always retain the opening medium/purpose contract before selected details.
    opening = lines[:2]
    return list(dict.fromkeys((*opening, *selected)))


def _adapt_yandex(brief: CreativeBrief) -> CreativeBrief:
    priority = (
        "Render the owner's requested scene faithfully. Preserve every explicit "
        "subject, action, relationship and state change."
    )
    prompt = _bounded_join(
        [priority, *_priority_lines(brief)],
        limit=_YANDEX_PROMPT_LIMIT,
    )
    return replace(brief, prompt=prompt)


def _adapt_yandex_motion(brief: CreativeBrief) -> CreativeBrief:
    keyframe = (
        "Create a keyframe that visibly contains the requested subject, interaction "
        "and direction of change; it will be animated afterwards."
    )
    prompt = _bounded_join(
        [keyframe, *_priority_lines(brief)],
        limit=_YANDEX_PROMPT_LIMIT,
    )
    return replace(brief, prompt=prompt)


def _adapt_runway(brief: CreativeBrief) -> CreativeBrief:
    # Runway's provider contract in this gateway is capped to 1000 prompt characters.
    # Preserve semantic obligations and style explicitly instead of blindly slicing
    # the tail off a long compiled prompt.
    prefix = (
        "Follow this frozen scene contract exactly. Preserve subject identity, "
        "requested actions, relationships and state changes."
    )
    prompt = _bounded_join(
        [prefix, *_priority_lines(brief)],
        limit=_RUNWAY_PROMPT_LIMIT,
    )
    return replace(brief, prompt=prompt)


def adapt_visual_brief_for_provider(
    brief: CreativeBrief,
    *,
    provider: str,
) -> CreativeBrief:
    value = brief.normalized()
    name = str(provider or "").strip().lower()
    if name == "yandexart":
        return _adapt_yandex(value)
    if name == "yandexart_motion":
        return _adapt_yandex_motion(value)
    if name == "runway":
        return _adapt_runway(value)
    # OpenAI, GigaChat and self-hosted gateways receive the frozen compiled prompt
    # unchanged. Provider adapters must not rewrite semantics without a concrete
    # provider constraint that requires it.
    return value


__all__ = ["PROMPT_ADAPTER_VERSION", "adapt_visual_brief_for_provider"]
