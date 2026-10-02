from __future__ import annotations

"""Provider-specific prompt shaping that preserves ClientPlatform semantic intent.

The application compiler owns meaning. This adapter may only reshape that frozen
meaning for a provider's prompt contract; it must never invent a new subject,
offer, claim, audience or transformation.
"""

from dataclasses import replace
import re

from .models import CreativeBrief


PROMPT_ADAPTER_VERSION = 5

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
        "autonomous composition",
        "autonomous video staging",
        "visible-state translation",
        "autonomous supporting detail",
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


_NUMBERED_DIRECTIVE_RE = re.compile(r"^\d+\.\s*")
_NUMBERED_DIRECTIVE_SPLIT_RE = re.compile(r"(?<!\S)(?=\d+\.\s)")
_OWNER_REQUEST_PREFIX = 'Owner request, preserve its meaning exactly: "'
_STYLE_SECTION_START = "style choices may shape presentation"
_STYLE_SECTION_END = "use credible natural details"


def _natural_safety_parts(brief: CreativeBrief) -> tuple[str, ...]:
    """Preserve production visual constraints without leaking compiler meta-language."""

    folded_prompt = " ".join(str(brief.prompt or "").casefold().split())
    folded_negative = " ".join(str(brief.negative_prompt or "").casefold().split())
    parts: list[str] = []

    if "no watermarks" in folded_prompt or "watermark" in folded_negative:
        parts.append("Без водяных знаков.")
    if (
        "do not invent brand logos or certifications" in folded_prompt
        or "invented logo" in folded_negative
    ):
        parts.append("Без выдуманных логотипов, сертификатов и знаков доверия.")
    if (
        "safe-area edges" in folded_prompt
        or "cropped important subject" in folded_negative
    ):
        parts.append("Все важные объекты полностью в кадре, с безопасными полями.")
    if (
        "no readable text, letters, captions or ui" in folded_prompt
        or "readable advertising text baked into image" in folded_negative
    ):
        parts.append("Без читаемого текста, подписей и элементов интерфейса.")

    return tuple(parts)


def _compiled_directives(prompt: str) -> tuple[str, ...]:
    # CreativeBrief.normalized() intentionally collapses all whitespace before the
    # provider adapter runs. Compiler v2 output therefore arrives as
    # "1. ... 2. ... 3. ..." rather than newline-separated directives.
    text = " ".join(str(prompt or "").split()).strip()
    if not text:
        return ()
    parts = _NUMBERED_DIRECTIVE_SPLIT_RE.split(text)
    return tuple(
        cleaned
        for part in parts
        if (cleaned := _NUMBERED_DIRECTIVE_RE.sub("", part).strip())
    )


def _compiled_owner_request(lines: tuple[str, ...]) -> str:
    for line in lines:
        if not line.startswith(_OWNER_REQUEST_PREFIX):
            continue
        value = line[len(_OWNER_REQUEST_PREFIX) :].strip()
        # Compiler v2 itself emits a bare closing quote. Some frozen/test-era
        # payloads also carry ordinary sentence punctuation after that quote.
        if value.endswith('".'):
            value = value[:-1].rstrip()
        if value.endswith('"'):
            return value[:-1].strip()
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
        if (
            folded.startswith(_STYLE_SECTION_END)
            or folded.startswith("business grounding")
            or folded.startswith("readable text is explicitly")
            or folded.startswith("do not rely on readable text")
        ):
            break
        if folded.startswith("combine every selected quick style accent"):
            continue
        selected.append(line)
    return tuple(selected)


def _compiled_semantic_visual_cues(
    lines: tuple[str, ...],
    *,
    kind: str,
) -> tuple[str, ...]:
    """Translate compiler semantics into short, natural provider-visible scene cues.

    Alice AI ART should not receive compiler control language such as "mandatory",
    but dropping those directives entirely loses the visible verbs and state changes
    that distinguish the owner's request from a generic portrait.
    """

    folded = tuple(line.casefold() for line in lines)
    cues: list[str] = []

    def has(prefix: str) -> bool:
        return any(line.startswith(prefix) for line in folded)

    if has("if the subject is listening"):
        cues.append(
            "Явно видно, что герой слушает аудио: наушники, колонка или устройство."
        )
    if has("if the subject is watching"):
        cues.append("Явно видна связь взгляда персонажа с экраном или источником.")
    if has("if the subject is reading"):
        cues.append("Явно видны материал для чтения и внимание персонажа к нему.")
    if has("if the request says the subject uses or interacts"):
        cues.append("Явно видно взаимодействие персонажа с указанным объектом.")
    if has("if the subject holds or carries"):
        cues.append("Указанный предмет явно и правдоподобно удерживается персонажем.")
    if has("if eating or drinking is requested"):
        cues.append("Явно видно само действие еды или питья и его источник.")
    if has("if the request contains another action"):
        cues.append("Запрошенное действие явно видно в кадре, это не статичный портрет.")

    if has("the transformation is mandatory") or has(
        "the transformation is a mandatory"
    ):
        if str(kind or "").strip().lower() == "video":
            cues.append(
                "Тот же герой проходит видимое изменение от исходного состояния "
                "через действие к ясному финалу."
            )
        else:
            cues.append(
                "Тот же герой: ясно различимы исходное состояние и результат изменения."
            )
    elif has("respect the requested chronology") or has("the request contains a sequence"):
        cues.append("Причинно-следственная последовательность действий ясно читается.")

    if has("visible-state translation"):
        cues.append(
            "Эмоции и качества видны по выражению, позе и фактуре, не по надписям."
        )
    if has("autonomous composition default: use a narrative story-scene"):
        cues.append("Сюжетная сцена; запрошенное действие — главный фокус.")
    elif has("autonomous composition default: use a balanced medium"):
        cues.append("Сбалансированная композиция с одним ясным главным объектом.")

    return tuple(dict.fromkeys(cues))


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
    parts.extend(_natural_safety_parts(brief))
    parts.extend(_compiled_style_directives(lines))
    if str(brief.brand_context or "").strip():
        parts.append("Контекст бренда: " + " ".join(str(brief.brand_context).split()))
    return parts


def _bounded_yandex_prompt(
    *,
    scene_head: str,
    semantic_cues: tuple[str, ...],
    brief: CreativeBrief,
    extras: tuple[str, ...] = (),
) -> str:
    """Preserve meaning and safety inside Alice's hard prompt limit.

    Long owner text is clipped before semantic evidence or safety is sacrificed.
    This keeps the visual verbs/state changes that make a scene faithful instead
    of allowing a long request to degrade back into a generic portrait.
    """

    safety = _natural_safety_parts(brief)
    safety_block = _bounded_join(list(safety), limit=_YANDEX_PROMPT_LIMIT)
    minimum_scene_head = 80
    semantic_budget = max(
        80,
        _YANDEX_PROMPT_LIMIT
        - len(safety_block)
        - (1 if safety_block else 0)
        - minimum_scene_head
        - 1,
    )
    bounded_semantics = _bounded_join(list(semantic_cues), limit=semantic_budget)
    semantic_parts = tuple(
        line for line in bounded_semantics.splitlines() if line.strip()
    )
    reserved = (
        len(safety_block)
        + len(bounded_semantics)
        + (1 if safety_block else 0)
        + (1 if bounded_semantics else 0)
    )
    scene_limit = max(minimum_scene_head, _YANDEX_PROMPT_LIMIT - reserved)
    bounded_scene_head = _bounded_join([scene_head], limit=scene_limit)
    return _bounded_join(
        [bounded_scene_head, *semantic_parts, *safety, *extras],
        limit=_YANDEX_PROMPT_LIMIT,
    )


def _adapt_yandex(brief: CreativeBrief) -> CreativeBrief:
    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)
    if not owner_request:
        prompt = _bounded_join([brief.prompt], limit=_YANDEX_PROMPT_LIMIT)
        return replace(brief, prompt=prompt)

    extras = list(_compiled_style_directives(lines))
    if str(brief.brand_context or "").strip():
        extras.append(
            "Контекст бренда: " + " ".join(str(brief.brand_context).split())
        )
    semantic_cues = _compiled_semantic_visual_cues(lines, kind=brief.kind)
    prompt = _bounded_yandex_prompt(
        scene_head=owner_request,
        semantic_cues=semantic_cues,
        brief=brief,
        extras=tuple(extras),
    )
    return replace(brief, prompt=prompt)


def _adapt_yandex_motion(brief: CreativeBrief) -> CreativeBrief:
    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)
    extras: list[str] = []
    semantic_cues: tuple[str, ...] = ()
    if owner_request:
        scene = "Ключевой кадр для короткого вертикального видео: " + owner_request
        semantic_cues = _compiled_semantic_visual_cues(lines, kind="image")
        extras.extend(_compiled_style_directives(lines))
        if str(brief.brand_context or "").strip():
            extras.append(
                "Контекст бренда: " + " ".join(str(brief.brand_context).split())
            )
    else:
        # Provider-direct/legacy requests are already natural scene descriptions.
        scene = (
            "Ключевой кадр для короткого вертикального видео: "
            + " ".join(str(brief.prompt or "").split())
        )
    prompt = _bounded_yandex_prompt(
        scene_head=scene,
        semantic_cues=semantic_cues,
        brief=brief,
        extras=tuple(extras),
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
        [prefix, *_natural_safety_parts(brief), *_priority_lines(brief)],
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
