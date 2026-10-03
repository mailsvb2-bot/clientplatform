from __future__ import annotations

"""Provider-neutral semantic prompt compiler for owner-authored visual requests.

Owners are expected to write short natural-language requests. The compiler turns
those requests into an explicit visual contract before any paid image/video job
is submitted. It is deliberately deterministic: retries and restarts compile the
same request into the same provider prompt and never require a second LLM call.
"""

from dataclasses import dataclass
import re

from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    resolve_visual_style_intent,
    visual_style_prompt_directives,
)


_MAX_REQUEST_CHARS = 1500
_MAX_BRAND_CONTEXT_CHARS = 1200
_MAX_COMPILED_PROMPT_CHARS = 12000
_MAX_NEGATIVE_PROMPT_CHARS = 1800

_TRANSFORMATION_RE = re.compile(
    r"(?:"
    r"превращ|станов|меняет(?:ся)?|изменяет(?:ся)?|до\s*(?:и|/|→|->)\s*после|"
    r"transform|turns?\s+into|becomes?|changes?\s+from|before\s*(?:and|/|→|->)\s*after"
    r")",
    re.IGNORECASE,
)
_OBJECT_REPLACEMENT_RE = re.compile(
    r"(?:"
    r"\bзамен\w*|\bпоменя\w*|\bсмен\w*|"
    r"\breplac(?:e|es|ed|ing|ement)\b|\bswap(?:s|ped|ping)?\b"
    r")",
    re.IGNORECASE,
)
_ABSTRACT_REPLACEMENT_TARGET_RE = re.compile(
    r"^\s*(?:"
    r"(?:цвет|цвета|цвету|цветом|цвете)\b(?:\s+(?:фона|фон)\b)?|"
    r"(?:стиль|стиля|стилю|стилем|стиле)\b|"
    r"(?:настроение|настроения|настроению|настроением|настроении)\b|"
    r"(?:тон|тона|тону|тоном|тоне)\b|"
    r"(?:фон|фона|фону|фоном|фоне)\b|"
    r"(?:освещение|освещения|освещению|освещением|освещении)\b|"
    r"(?:контраст|контраста|контрасту|контрастом|контрасте)\b|"
    r"(?:композиция|композиции|композицию|композицией)\b|"
    r"(?:шрифт|шрифта|шрифту|шрифтом|шрифте)\b|"
    r"(?:текст|текста|тексту|текстом|тексте)\b|"
    r"(?:ракурс|ракурса|ракурсу|ракурсом|ракурсе)\b|"
    r"(?:формат|формата|формату|форматом|формате)\b|"
    r"(?:палитра|палитры|палитру|палитрой)\b|"
    r"(?:атмосфера|атмосферы|атмосферу|атмосферой)\b|"
    r"(?:the\s+)?(?:visual\s+)?(?:color|style|mood|tone|background|lighting|"
    r"contrast|composition|font|text|angle|format|palette)\b"
    r")",
    re.IGNORECASE,
)
_SEQUENCE_RE = re.compile(
    r"(?:сначала|затем|потом|после|вначале|в\s+конце|"
    r"first|then|after|finally|at\s+the\s+end)",
    re.IGNORECASE,
)
_LISTENING_RE = re.compile(
    r"(?:\bслуша\w*|\bнаушник\w*|\bаудиосес\w*|\bподкаст\w*|"
    r"\blisten(?:s|ing)?\b|\bheadphones?\b|\bearbuds?\b|\bpodcasts?\b)",
    re.IGNORECASE,
)
_WATCHING_RE = re.compile(
    r"(?:\bсмотр\w*|\bпросматрива\w*|\bэкран\w*|"
    r"\bwatch(?:es|ing)?\b|\bviewing\b|\bscreens?\b)",
    re.IGNORECASE,
)
_READING_RE = re.compile(
    r"(?:\bчит\w*|\bкниг\w*|\bread(?:s|ing)?\b|\bbooks?\b|\barticles?\b)",
    re.IGNORECASE,
)
_USING_RE = re.compile(
    r"(?:\bиспользу\w*|\bпользу\w*|\bприменя\w*|\bработа\w*\s+с\b|"
    r"\buses?\b|\busing\b|\binteracts?\s+with\b)",
    re.IGNORECASE,
)
_HOLDING_RE = re.compile(
    r"(?:\bдерж\w*|\bнес[её]\w*|\bбер[её]\w*|"
    r"\bhold(?:s|ing)?\b|\bcarr(?:y|ies|ying)\b)",
    re.IGNORECASE,
)
_EATING_RE = re.compile(
    r"(?:\bест\b|\bедят\b|\bкуша\w*|\bпь[её]\w*|\bвыпива\w*|"
    r"\beat(?:s|ing)?\b|\bdrink(?:s|ing)?\b)",
    re.IGNORECASE,
)
_GENERIC_ACTION_RE = re.compile(
    r"(?:"
    r"\bбеж\w*|\bид[её]т\b|\bидут\b|\bтанцу\w*|\bулыба\w*|\bплач\w*|"
    r"\bговор\w*|\bпиш\w*|\bрису\w*|\bработа(?:ет|ют|ющий|ющая|ющее|ющие)\w*|"
    r"\bигра(?:ет|ют|ющий|ющая|ющее|ющие)\w*|\bобнима\w*|"
    r"\bоткрыва\w*|\bзакрыва\w*|\bмо[её]т\w*|\bчинит\w*|\bремонтиру\w*|"
    r"\bготовит\w*|\bедет\b|\bедут\b|\bлетит\b|\bлетят\b|"
    r"\brun(?:s|ning)?\b|\bwalk(?:s|ing)?\b|\bdanc(?:e|es|ing)\b|"
    r"\bsmil(?:e|es|ing)\b|\bcr(?:y|ies|ying)\b|\bspeak(?:s|ing)?\b|"
    r"\bwrit(?:e|es|ing)\b|\bdraw(?:s|ing)?\b|\bwork(?:s|ing)?\b|"
    r"\bplay(?:s|ing)?\b|\bhug(?:s|ging)?\b|\bopen(?:s|ing)?\b|"
    r"\bclos(?:e|es|ing)\b|\bwash(?:es|ing)?\b|\brepair(?:s|ing)?\b|"
    r"\bcook(?:s|ing)?\b|\bdriv(?:e|es|ing)\b|\bfl(?:y|ies|ying)\b"
    r")",
    re.IGNORECASE,
)
_VISIBLE_STATE_RE = re.compile(
    r"(?:"
    r"\bдобр\w*|\bзл\w*|\bспокойн\w*|\bтревож\w*|\bсчастлив\w*|"
    r"\bгруст\w*|\bпушист\w*|\bмягк\w*|\bколюч\w*|\bгрязн\w*|"
    r"\bчист\w*|\bблестящ\w*|\bуверенн\w*|\bиспуган\w*|"
    r"\bkind\b|\bgentle\b|\bcalm\b|\bangry\b|\bhappy\b|\bsad\b|"
    r"\bfluffy\b|\bsoft\b|\bprickly\b|\bdirty\b|\bclean\b|"
    r"\bshiny\b|\bconfident\b|\bafraid\b"
    r")",
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

_PORTRAIT_RE = re.compile(
    r"(?:\bпортрет\w*|\bхедшот\w*|\bportrait\b|\bheadshot\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompiledVisualPrompt:
    prompt: str
    negative_prompt: str
    semantic_flags: tuple[str, ...]
    style_intent: VisualStyleIntent


_SEMANTIC_QA_CONTRACT_VERSION = 1
_SEMANTIC_QA_FLAGS = frozenset(
    {
        "transformation",
        "object_replacement",
        "sequence",
        "listening",
        "watching",
        "reading",
        "using",
        "holding",
        "eating_or_drinking",
        "generic_action",
        "comparison",
        "explicit_text",
        "portrait",
        "visible_state",
    }
)


@dataclass(frozen=True, slots=True)
class VisualSemanticQAContract:
    """Immutable meaning contract for advisory post-generation image review."""

    version: int
    kind: str
    country_code: str
    owner_request: str
    semantic_flags: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "country_code": self.country_code,
            "owner_request": self.owner_request,
            "semantic_flags": list(self.semantic_flags),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "VisualSemanticQAContract":
        if not isinstance(value, dict) or set(value) != {
            "version",
            "kind",
            "country_code",
            "owner_request",
            "semantic_flags",
        }:
            raise ValueError("visual semantic QA contract is invalid")
        version = value.get("version")
        kind = str(value.get("kind") or "").strip().lower()
        country_code = str(value.get("country_code") or "").strip().upper()
        if (
            len(country_code) > 16
            or any(not (char.isalnum() or char in {"-", "_"}) for char in country_code)
        ):
            raise ValueError("visual semantic QA contract is invalid")
        owner_request = _clean(
            str(value.get("owner_request") or ""),
            field="request",
            limit=_MAX_REQUEST_CHARS,
        )
        raw_flags = value.get("semantic_flags")
        if (
            version != _SEMANTIC_QA_CONTRACT_VERSION
            or kind != "image"
            or not isinstance(raw_flags, list)
            or len(raw_flags) > len(_SEMANTIC_QA_FLAGS)
        ):
            raise ValueError("visual semantic QA contract is invalid")
        flags = tuple(str(item or "").strip() for item in raw_flags)
        if (
            len(set(flags)) != len(flags)
            or any(not item or item not in _SEMANTIC_QA_FLAGS for item in flags)
        ):
            raise ValueError("visual semantic QA contract is invalid")
        return cls(
            version=_SEMANTIC_QA_CONTRACT_VERSION,
            kind="image",
            country_code=country_code,
            owner_request=owner_request,
            semantic_flags=flags,
        )


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
        ("generic_action", _GENERIC_ACTION_RE),
        ("comparison", _COMPARISON_RE),
        ("explicit_text", _TEXT_REQUEST_RE),
        ("portrait", _PORTRAIT_RE),
    )
    flags = [name for name, pattern in checks if pattern.search(request)]
    replacement_matches = tuple(_OBJECT_REPLACEMENT_RE.finditer(request))
    physical_replacement = any(
        not _ABSTRACT_REPLACEMENT_TARGET_RE.match(request[match.end() :])
        for match in replacement_matches
    )
    if physical_replacement:
        # Classify the target governed by each replacement operator instead of using
        # a finite object allowlist. This keeps arbitrary physical nouns (pipe, car,
        # flower, street lamp, etc.) in replacement mode while "change the style",
        # "background color replacement" and similar presentation edits stay out.
        # In mixed requests, any physical replacement is preserved even if another
        # clause also changes an abstract presentation property.
        insert_at = 1 if "transformation" in flags else 0
        flags.insert(insert_at, "object_replacement")
    # Descriptive words such as "calm" may refer only to visual style in a static
    # request. Treat them as state evidence only when the owner actually asks for
    # a transformation, so autopilot does not invent a character-state narrative.
    if "transformation" in flags and _VISIBLE_STATE_RE.search(request):
        flags.append("visible_state")
    return tuple(flags)


def build_visual_semantic_qa_contract(
    *,
    request: str,
    kind: str,
    country_code: str = "",
) -> VisualSemanticQAContract | None:
    """Freeze the same semantic flags used by the prompt compiler for image QA.

    Video review is intentionally out of scope for this first slice. Returning
    None keeps video generation compatible and avoids silently adding another
    paid provider call to an existing video consent flow.
    """

    visual_kind = str(kind or "").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    if visual_kind != "image":
        return None
    owner_request = _clean(
        request,
        field="request",
        limit=_MAX_REQUEST_CHARS,
    )
    return VisualSemanticQAContract(
        version=_SEMANTIC_QA_CONTRACT_VERSION,
        kind="image",
        country_code=str(country_code or "").strip().upper(),
        owner_request=owner_request,
        semantic_flags=_semantic_flags(owner_request),
    )


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
    if "generic_action" in flags:
        directives.append(
            "If the request contains another action, make that action visually "
            "legible through body pose, contact, motion cues and relevant objects; "
            "do not reduce it to a static portrait or symbolic substitute."
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
        "The transformation is mandatory visual evidence, not optional mood. In a "
        "single image, deliberately repeat the same subject as a compact visual "
        "storyboard: BEFORE, the causal action or interaction, then AFTER. Repetition "
        "of the same subject is intentional; never collapse the request to one "
        "final-state portrait.",
        "If the owner did not define an initial state, use a neutral ordinary baseline "
        "instead of inventing an extreme opposite. Keep identity continuity across "
        "stages. Make every requested changed quality visibly stronger in the AFTER "
        "state through expression, posture, texture, material condition or grooming, "
        "and keep the causal action spatially connected to the transition.",
        "Transformation stage detail: make every stage independently readable as a "
        "visual scene. The opening stage shows the stated initial condition, or a "
        "neutral ordinary baseline when none was stated. The middle stage shows the "
        "concrete cause/action plus the first visible signs of change. The final stage "
        "shows every requested changed quality through concrete expression, posture, "
        "texture, material condition or grooming. Stage names are prompt structure "
        "only. Do not render BEFORE/AFTER words, panel labels, arrows, numbers or "
        "captions unless the owner explicitly requested those exact elements as "
        "visible text.",
    ]


def _replacement_directives(kind: str, flags: tuple[str, ...]) -> list[str]:
    if "object_replacement" not in flags:
        return []
    if kind == "video":
        return [
            "Object replacement is the core event. Preserve the surrounding scene and "
            "show a clear, physically plausible replacement of only the requested "
            "object. End on the completed usable installation; do not redesign unrelated "
            "parts of the environment.",
            "For functional fixtures, appliances or furniture, keep every necessary "
            "visible component, control, support and connection coherent. Do not show "
            "an incomplete showroom prop when the request implies an installed result.",
        ]
    return [
        "Treat object replacement as a constrained replacement event, not as a request "
        "for an unrelated new interior or a catalog shot of the final object. Preserve "
        "the surrounding environment and replace only the requested object unless the "
        "owner explicitly asks for broader redesign.",
        "Make the replacement itself visually legible. If no reference image is "
        "available downstream, show either the installation action or a clear before/"
        "after in the same environment; do not show only a finished isolated object.",
        "Show the replacement as complete and physically coherent. When the request "
        "implies an installed result, make it visibly installed and usable. For "
        "functional fixtures, appliances or furniture, include the necessary visible "
        "controls, supports, mounting and connections and keep geometry, scale, shadows "
        "and contact with surrounding surfaces believable.",
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


def _autonomous_scene_directives(
    *,
    kind: str,
    flags: tuple[str, ...],
    style: VisualStyleIntent,
) -> list[str]:
    """Fill production details automatically without changing the owner's meaning."""

    directives = [
        "Autonomous supporting detail: infer the environment, camera distance, "
        "supporting props and lighting conservatively. Add only details that make "
        "the requested subject, action, relationship or state easier to read; never "
        "replace the owner's idea with decorative stock imagery.",
    ]
    dynamic_flags = {
        "listening",
        "watching",
        "reading",
        "using",
        "holding",
        "eating_or_drinking",
        "generic_action",
        "object_replacement",
        "sequence",
        "transformation",
    }
    has_action = bool(dynamic_flags.intersection(flags))

    if style.composition == "auto":
        if kind == "image" and "object_replacement" in flags:
            directives.append(
                "Autonomous composition default: show the requested replacement in its "
                "real surrounding context as a complete installed result. Keep the "
                "environment stable so the changed object is immediately identifiable."
            )
        elif kind == "image" and "transformation" in flags:
            directives.append(
                "Autonomous composition default: use a compact storyboard, triptych "
                "or paired transformation composition. The same subject may appear "
                "more than once intentionally so BEFORE, cause/action and AFTER are "
                "all readable in the single image."
            )
        elif kind == "image" and has_action and "portrait" not in flags:
            directives.append(
                "Autonomous composition default: use a narrative story-scene rather "
                "than a catalog portrait, with the requested action as the focal event."
            )
        elif "portrait" in flags:
            directives.append(
                "Autonomous composition default: use a clean close or medium portrait "
                "while preserving every explicitly requested prop or interaction."
            )
        elif kind == "video":
            directives.append(
                "Autonomous composition default: use a readable story-scene with a "
                "clear focal subject and enough environment to understand the action."
            )
        else:
            directives.append(
                "Autonomous composition default: use a balanced medium story-oriented "
                "composition with one clear focal subject and no arbitrary empty bands."
            )

    if "visible_state" in flags or "transformation" in flags:
        directives.append(
            "Visible-state translation: turn abstract qualities, emotions and state "
            "changes into concrete visual evidence such as facial expression, posture, "
            "gesture, texture, material condition, grooming and lighting. Never rely "
            "on captions, labels or generic symbols to explain the change."
        )

    if kind == "video":
        directives.append(
            "Autonomous video staging: prefer one coherent shot or only necessary "
            "cuts; establish the subject, show the requested action clearly, and end "
            "with the requested result instead of spending time on decorative motion."
        )

    return directives


def _autonomous_quality_directives(
    *,
    kind: str,
    purpose: str,
    style: VisualStyleIntent,
) -> list[str]:
    """Provide sensible defaults only for dimensions the owner left on auto."""

    directives: list[str] = []
    if style.lighting == "auto":
        directives.append(
            "Autonomous lighting default: use coherent balanced lighting that clearly "
            "reveals the subject, action and important textures."
        )
    if style.contrast == "auto":
        directives.append(
            "Autonomous contrast default: keep foreground/background separation clear "
            "without crushing shadows or washing out important details."
        )
    if style.detail == "auto":
        directives.append(
            "Autonomous detail default: use rich but controlled detail; prioritize "
            "anatomy, materials, expression and interaction over decorative clutter."
        )
    if style.realism == "auto" and not style.quick_style_names():
        directives.append(
            "Autonomous rendering default: choose a polished believable visual language "
            "appropriate to the subject; avoid a generic stock or clip-art appearance."
        )
    if style.commercial_tone == "auto":
        directives.append(
            "Autonomous presentation default: "
            + (
                "keep advertising polish professional, human and non-generic."
                if purpose == "advertising"
                else "keep the result visually polished and natural rather than salesy."
            )
        )
    if kind == "video" and style.motion == "auto":
        directives.append(
            "Autonomous motion default: use purposeful, stable motion that supports "
            "the requested action; avoid random camera movement."
        )
    return directives


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
    style_intent: VisualStyleIntent | None = None,
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
    resolved_style = resolve_visual_style_intent(
        request=owner_request,
        selected=style_intent,
    )
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
        *_interaction_directives(flags),
        *_transformation_directives(visual_kind, flags),
        *_replacement_directives(visual_kind, flags),
        *_sequence_directives(visual_kind, flags),
        *_autonomous_scene_directives(
            kind=visual_kind,
            flags=flags,
            style=resolved_style,
        ),
        *_business_context_directive(brand),
        "Style choices may shape presentation but must never remove or contradict "
        "mandatory subjects, actions, relationships, chronology or state changes.",
        *visual_style_prompt_directives(resolved_style, kind=visual_kind),
        *_autonomous_quality_directives(
            kind=visual_kind,
            purpose=visual_purpose,
            style=resolved_style,
        ),
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
            "For real-world functional objects, preserve physical completeness and "
            "ordinary usability: do not omit essential controls, supports, openings, "
            "mounting or connections merely for a cleaner-looking composition.",
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
        if brand:
            directives.append(
                "Names from business grounding are semantic context only. Never render "
                "those names as signs, labels, logos, captions or decorative lettering "
                "unless the owner explicitly requested that exact visible text."
            )

    prompt = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(directives))
    if len(prompt) > _MAX_COMPILED_PROMPT_CHARS:
        prompt = prompt[:_MAX_COMPILED_PROMPT_CHARS].rstrip()

    dynamic_flags = {
        "transformation",
        "sequence",
        "listening",
        "watching",
        "reading",
        "using",
        "holding",
        "eating_or_drinking",
        "generic_action",
        "object_replacement",
    }
    has_dynamic_action = bool(dynamic_flags.intersection(flags))
    negatives = [
        "unrelated props",
        "unrelated replacement subject",
        "cropped important subject",
        "large blank technical band",
        "watermark",
        "gibberish text",
        "invented logo",
    ]
    if has_dynamic_action:
        negatives.extend(
            [
                "static catalog shot when an action was requested",
                "missing requested action",
                "missing requested relationship",
            ]
        )
        if "portrait" not in flags:
            negatives.append("generic isolated portrait")
    if "object_replacement" in flags:
        negatives.extend(
            [
                "unrelated room redesign instead of requested replacement",
                "physically incomplete replacement object",
                "missing essential functional hardware or controls",
                "floating or disconnected installed fixture",
            ]
        )
    if "transformation" not in flags:
        negatives.append("duplicate main subject by accident")
    if "transformation" in flags:
        negatives.extend(
            [
                "single-state image with no visible transformation",
                "before and after shown as unrelated characters",
                "unchanged final state",
                "state change conveyed only by text or a generic symbol",
            ]
        )
        if not explicit_text:
            negatives.append("storyboard stage labels, arrows, numbers or captions")
    if "visible_state" in flags:
        negatives.extend(
            [
                "requested emotion or quality not visually readable",
                "final texture or expression inconsistent with requested state",
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
        style_intent=resolved_style,
    )


__all__ = [
    "CompiledVisualPrompt",
    "VisualSemanticQAContract",
    "build_visual_semantic_qa_contract",
    "compile_visual_prompt",
]
