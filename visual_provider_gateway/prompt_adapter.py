from __future__ import annotations

"""Provider-specific prompt shaping that preserves ClientPlatform semantic intent.

The application compiler owns meaning. This adapter may only reshape that frozen
meaning for a provider's prompt contract; it must never invent a new subject,
offer, claim, audience or transformation.
"""

from dataclasses import replace
import re

from .models import CreativeBrief


PROMPT_ADAPTER_VERSION = 3

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


_NUMBERED_DIRECTIVE_RE = re.compile(r"^\\d+\\.\\s*")
_OWNER_REQUEST_PREFIX = 'Owner request, preserve its meaning exactly: "'
_STYLE_SECTION_START = "style choices may shape presentation"
_STYLE_SECTION_END = "use credible natural details"


def _compiled_directives(prompt: str) -> tuple[str, ...]:
    return tuple(
        _NUMBERED_DIRECTIVE_RE.sub("", line).strip()
        for line in _lines(prompt)
        if _NUMBERED_DIRECTIVE_RE.sub("", line).strip()
    )


def _compiled_owner_request(lines: tuple[str, ...]) -> str:
    for line in lines:
        if not line.startswith(_OWNER_REQUEST_PREFIX) or not line.endswith('"'):
            continue
        return line[len(_OWNER_REQUEST_PREFIX) : -1].strip()
    return ""


def _compiled_style_directives(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Return only concrete visual-style sentences from compiler v2 output."""

    inside = False
    selected: list[str] = []
    for line in lines:
        folded = line.casefold()
        if folded.startswith(_STYLE_SECTION_START):
            inside = True
            continue
        if not inside:
            continue
        if folded.startswith(_STYLE_SECTION_END):
            break
        selected.append(line)
    return tuple(selected)


def _yandex_natural_prompt_parts(brief: CreativeBrief) -> list[str]:
    """Shape compiler output as a natural image description for Alice AI ART.

    Alice's Images API expects an image description. Provider-neutral compiler
    control language (owner request, mandatory contract, do-not-invent rules)
    can be interpreted as conversational/meta instructions and rejected by the
    image model even when the underlying scene is harmless.
    """

    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)
    if not owner_request:
        # Legacy/provider-direct briefs are already natural prompts.
        return [brief.prompt]

    parts = [owner_request]
    parts.extend(_compiled_style_directives(lines))
    if str(brief.brand_context or "").strip():
        parts.append("Контекст бренда: " + " ".join(str(brief.brand_context).split()))
    return parts


def _adapt_yandex(brief: CreativeBrief) -> CreativeBrief:
    prompt = _bounded_join(
        _yandex_natural_prompt_parts(brief),
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
