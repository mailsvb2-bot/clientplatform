from __future__ import annotations

"""Provider-neutral semantic prompt compiler for owner-authored visual requests.

Owners are expected to write short natural-language requests. The compiler turns
those requests into an explicit visual contract before any paid image/video job
is submitted. It is deliberately deterministic: retries and restarts compile the
same request into the same provider prompt and never require a second LLM call.
"""

from dataclasses import dataclass
import re


_MAX_REQUEST_CHARS = 1500
_MAX_BRAND_CONTEXT_CHARS = 1200
_MAX_COMPILED_PROMPT_CHARS = 5200
_MAX_NEGATIVE_PROMPT_CHARS = 1800

_TRANSFORMATION_RE = re.compile(
    r"(?:"
    r"превращ|станов|меняет(?:ся)?|изменяет(?:ся)?|до\s*(?:и|/|→|->)\s*после|"
    r"из\s+.+?\s+в\s+|"
    r"transform|turns?\s+into|becomes?|changes?\s+from|before\s*(?:and|/|→|->)\s*after"
    r")",
    re.IGNORECASE,
)
_SEQUENCE_RE = re.compile(
    r"(?:сначала|затем|потом|после|вначале|в\s+конце|"
    r"first|then|after|finally|at\s+the\s+end)",
    re.IGNORECASE,
)
_LISTENING_RE = re.compile(
    r"(?:слуша|наушник|аудио|подкаст|музык|listen|headphone|earbud|audio|podcast|music)",
    re.IGNORECASE,
)
_WATCHING_RE = re.compile(
    r"(?:смотрит|просматривает|видео|экран|watch|viewing|screen|video)",
    re.IGNORECASE,
)
_READING_RE = re.compile(
    r"(?:читает|книг|текст|read(?:s|ing)?|book|article)",
    re.IGNORECASE,
)
_USING_RE = re.compile(
    r"(?:использует|пользуется|применяет|работает\s+с|"
    r"uses?|using|interacts?\s+with)",
    re.IGNORECASE,
)
_HOLDING_RE = re.compile(
    r"(?:держит|несет|берет|hold(?:s|ing)?|carries|carrying)",
    re.IGNORECASE,
)
_EATING_RE = re.compile(
    r"(?:ест|кушает|пьет|выпивает|eat(?:s|ing)?|drink(?:s|ing)?)",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"(?:слева|справа|две\s+части|сравнен|до\s+и\s+после|"
    r"left|right|split|comparison|before\s+and\s+after)",
    re.IGNORECASE,
)
_TEXT_REQUEST_RE = re.compile(
    r"(?:текст\s+на\s+(?:картинк|изображен)|надпись|подпись|"
    r"caption|text\s+on\s+(?:image|picture)|written\s+text)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompiledVisualPrompt:
    prompt: str
    negative_prompt: str
    semantic_flags: tuple[str, ...]


def _clean(value: str, *, field: str, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not text:
        if field == "brand_context":
            return ""
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} contains control characters")
    return text


def _semantic_flags(request: str) -> tuple[str, ...]:
    checks = (
        ("transformation", _TRANSFORMATION_RE),
        ("sequence", _SEQUENCE_RE),
        ("listening", _LISTENING_RE),
        ("watching", _WATCHING_RE),
        ("reading", _READING_RE),
        ("using", _USING_RE),
        ("holding", _HOLDING_RE),
        ("eating_or_drinking", _EATING_RE),
        ("comparison", _COMPARISON_RE),
        ("explicit_text", _TEXT_REQUEST_RE),
    )
    return tuple(name for name, pattern in checks if pattern.search(request))


def _interaction_directives(flags: tuple[str, ...]) -> list[str]:
    directives: list[str] = []
    if "listening" in flags:
        directives.append(
            "If the subject is listening, make the listening unmistakable through "
            "visible audio interaction such as headphones, earbuds, a speaker or a "
            "device playing audio; do not represent listening as a static portrait."
        )
    if "watching" in flags:
        directives.append(
            "If the subject is watching something, make the gaze and the viewed "
            "screen/source visibly connected in the composition."
        )
    if "reading" in flags:
        directives.append(
            "If the subject is reading, visibly show the reading material and the "
            "subject's attention to it; do not reduce the scene to a portrait."
        )
    if "using" in flags:
        directives.append(
            "If the request says the subject uses or interacts with something, show "
            "the physical or visual interaction clearly rather than merely placing "
            "the two objects in the same frame."
        )
    if "holding" in flags:
        directives.append(
            "If the subject holds or carries something, make that contact clearly "
            "visible and anatomically plausible."
        )
    if "eating_or_drinking" in flags:
        directives.append(
            "If eating or drinking is requested, show the action itself and its "
            "source object, not only the food, drink or subject separately."
        )
    return directives


def _transformation_directives(kind: str, flags: tuple[str, ...]) -> list[str]:
    if "transformation" not in flags:
        return []
    if kind == "video":
        return [
            "The transformation is a mandatory story beat. Preserve the identity of "
            "the same main subject across the whole clip: establish the initial state, "
            "show the cause/action, make the change visibly happen, and end on the "
            "requested final state. Do not jump to an unrelated replacement subject.",
            "Use the 8-second timeline efficiently: opening state, interaction or "
            "transition, then a clearly readable final state.",
        ]
    return [
        "The transformation is mandatory visual evidence, not optional mood. Show "
        "both the initial and final states of the same subject in one coherent image. "
        "Prefer a clear split/paired composition or a continuous in-frame transition "
        "when that makes the change immediately understandable.",
        "Keep identity continuity between the before and after states. The viewer "
        "must understand what changed without relying on explanatory text.",
    ]


def _sequence_directives(kind: str, flags: tuple[str, ...]) -> list[str]:
    if "sequence" not in flags or "transformation" in flags:
        return []
    if kind == "video":
        return [
            "Respect the requested chronology. Stage the stated actions in their "
            "original order and make each transition visually legible."
        ]
    return [
        "The request contains a sequence. Compress it into one readable story image "
        "using a clear multi-stage or cause-and-effect composition instead of "
        "dropping later actions."
    ]


def _business_context_directive(brand_context: str) -> list[str]:
    if not brand_context:
        return []
    return [
        "Business grounding (context only; do not invent facts beyond it): "
        f"{brand_context}",
        "Treat unfamiliar names in the owner request as possible names of this "
        "business, product, service, method, session or content. Do not discard them "
        "just because the image model may not know the term. Depict the requested "
        "interaction in a plausible visual way and use business grounding only to "
        "disambiguate meaning.",
    ]


def compile_visual_prompt(
    *,
    request: str,
    kind: str,
    brand_context: str = "",
    purpose: str = "owner_visual",
) -> CompiledVisualPrompt:
    owner_request = _clean(
        request,
        field="request",
        limit=_MAX_REQUEST_CHARS,
    )
    visual_kind = str(kind or "").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    brand = (
        _clean(
            brand_context,
            field="brand_context",
            limit=_MAX_BRAND_CONTEXT_CHARS,
        )
        if str(brand_context or "").strip()
        else ""
    )
    visual_purpose = str(purpose or "owner_visual").strip().lower()
    if visual_purpose not in {"owner_visual", "advertising"}:
        raise ValueError("visual purpose is invalid")

    flags = _semantic_flags(owner_request)
    explicit_text = "explicit_text" in flags
    if visual_kind == "video":
        medium = (
            "Create one polished short vertical advertising video as an 8-second "
            "visual story."
            if visual_purpose == "advertising"
            else "Create one polished 8-second vertical visual story."
        )
    else:
        medium = "Create one polished single advertising-quality image."
    purpose_line = (
        "The visual may be used in advertising, but semantic fidelity to the owner's "
        "idea is more important than making a generic commercial stock image."
        if visual_purpose == "advertising"
        else "Semantic fidelity to the owner's idea is the primary objective."
    )

    directives = [
        medium,
        purpose_line,
        f'Owner request, preserve its meaning exactly: "{owner_request}"',
        "Interpret the request as a scene contract, not as a bag of keywords. Every "
        "explicit subject, action, relationship, state and state change is mandatory "
        "unless it is impossible to depict visually.",
        "Never collapse a multi-action request into a generic portrait of the main "
        "noun. Show visual evidence for the requested verbs and relationships.",
        *_business_context_directive(brand),
        *_interaction_directives(flags),
        *_transformation_directives(visual_kind, flags),
        *_sequence_directives(visual_kind, flags),
    ]

    if "comparison" in flags and "transformation" not in flags:
        directives.append(
            "The requested comparison must be visually explicit. Keep compared "
            "subjects or states easy to distinguish while preserving a coherent style."
        )

    directives.extend(
        [
            "Use credible natural details, coherent anatomy, realistic lighting and "
            "a clear visual hierarchy. Keep important subjects fully inside the frame "
            "with comfortable margins and use the whole canvas.",
            "Do not add fake awards, fake reviews, invented statistics, medical or "
            "money guarantees, manipulative urgency, or claims not present in the "
            "owner request or business grounding.",
        ]
    )
    if explicit_text:
        directives.append(
            "Readable text is explicitly part of the owner's concept. Keep it short, "
            "legible and limited to exactly what the owner requested; do not invent "
            "additional advertising copy."
        )
    else:
        directives.append(
            "Do not rely on readable text, labels, logos or captions to explain the "
            "scene. Communicate the idea visually; typography is handled separately."
        )

    prompt = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(directives))
    if len(prompt) > _MAX_COMPILED_PROMPT_CHARS:
        prompt = prompt[:_MAX_COMPILED_PROMPT_CHARS].rstrip()

    negatives = [
        "generic isolated portrait",
        "static catalog shot when an action was requested",
        "missing requested action",
        "missing requested relationship",
        "unrelated props",
        "unrelated replacement subject",
        "duplicate main subject by accident",
        "cropped important subject",
        "large blank technical band",
        "watermark",
        "gibberish text",
        "invented logo",
    ]
    if "transformation" in flags:
        negatives.extend(
            [
                "single-state image with no visible transformation",
                "before and after shown as unrelated characters",
                "unchanged final state",
            ]
        )
    if "listening" in flags:
        negatives.extend(
            [
                "listening implied only by facial expression",
                "audio interaction missing",
            ]
        )
    if not explicit_text:
        negatives.append("readable advertising text baked into image")
    negative_prompt = "; ".join(negatives)
    if len(negative_prompt) > _MAX_NEGATIVE_PROMPT_CHARS:
        negative_prompt = negative_prompt[:_MAX_NEGATIVE_PROMPT_CHARS].rstrip(" ;")

    return CompiledVisualPrompt(
        prompt=prompt,
        negative_prompt=negative_prompt,
        semantic_flags=flags,
    )


__all__ = ["CompiledVisualPrompt", "compile_visual_prompt"]
