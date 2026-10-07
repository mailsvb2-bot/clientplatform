from __future__ import annotations

"""Provider-specific prompt shaping that preserves ClientPlatform semantic intent.

The application compiler owns meaning. This adapter may only reshape that frozen
meaning for a provider's prompt contract; it must never invent a new subject,
offer, claim, audience or transformation.
"""

from dataclasses import replace
import re

from .models import CreativeBrief


PROMPT_ADAPTER_VERSION = 14

_RUNWAY_PROMPT_LIMIT = 1000
_YANDEX_PROMPT_LIMIT = 500
_GIGACHAT_PROMPT_LIMIT = 1800


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

    watermark = "no watermarks" in folded_prompt or "watermark" in folded_negative
    invented_logo = (
        "do not invent brand logos or certifications" in folded_prompt
        or "invented logo" in folded_negative
    )
    if watermark and invented_logo:
        parts.append("Без водяных знаков. Без выдуманных логотипов.")
    elif watermark:
        parts.append("Без водяных знаков.")
    elif invented_logo:
        parts.append("Без выдуманных логотипов.")

    if (
        "safe-area edges" in folded_prompt
        or "cropped important subject" in folded_negative
    ):
        parts.append("Главные объекты полностью в кадре.")
    explicit_text = "readable text is explicitly part" in folded_prompt
    if explicit_text:
        parts.append(
            "Показывай только явно запрошенный текст; без других надписей или букв."
        )
    elif (
        "no readable text, letters, captions or ui" in folded_prompt
        or "readable advertising text baked into image" in folded_negative
    ):
        parts.append("Без читаемого текста и UI.")
    if str(brief.brand_context or "").strip():
        parts.append(
            "Не печатай названия бренда/услуг/методов без явного запроса на это."
        )

    return tuple(parts)


def _yandex_safety_parts(brief: CreativeBrief) -> tuple[str, ...]:
    """Compact ordered constraints for Alice's 500-char prompt.

    Meaning and owner-selected style get first claim on the tiny provider budget.
    The most semantically important anti-lettering rules come first so truncation
    cannot keep decorative safeguards while dropping accidental-text protection.
    """

    natural = _natural_safety_parts(brief)
    joined = " ".join(natural)
    clauses: list[str] = []
    if "только явно запрошенный текст" in joined.casefold():
        clauses.append("Только запрошенный текст; без других надписей.")
    elif "Без читаемого текста" in joined:
        clauses.append("Без читаемого текста/UI.")
    if "названия бренда/услуг/методов" in joined.casefold():
        clauses.append("Названия бренда/услуг не печатать без явного запроса.")
    if "Без водяных знаков" in joined and "Без выдуманных логотипов" in joined:
        clauses.append("Без водяных знаков. Без выдуманных логотипов.")
    elif "Без водяных знаков" in joined:
        clauses.append("Без водяных знаков.")
    elif "Без выдуманных логотипов" in joined:
        clauses.append("Без выдуманных логотипов.")
    if "полностью в кадре" in joined:
        clauses.append("Главные объекты полностью в кадре.")
    return tuple(clauses)


def _natural_policy_parts(brief: CreativeBrief) -> tuple[str, ...]:
    """Preserve product-level truthfulness rules in provider-natural language."""

    folded_prompt = " ".join(str(brief.prompt or "").casefold().split())
    parts: list[str] = []
    if (
        "do not add fake awards, fake reviews, invented statistics" in folded_prompt
        or "medical or money guarantees" in folded_prompt
        or "manipulative urgency" in folded_prompt
    ):
        parts.append(
            "Не выдумывай награды, отзывы, статистику, гарантии, срочность "
            "или рекламные утверждения, которых нет в запросе."
        )
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


def _compiled_scene_direction_cue(lines: tuple[str, ...]) -> str:
    for line in lines:
        folded = line.casefold()
        if not folded.startswith("selected presentation direction"):
            continue
        value = line.split(":", 1)[-1].strip()
        base_value = value
        supplement = ""
        marker = ". Owner refinement: "
        if marker in value:
            base_value, raw_supplement = value.split(marker, 1)
            supplement = raw_supplement.split(
                ". Apply this only where compatible",
                1,
            )[0].strip(" .")
        lower = base_value.casefold()
        if "left-to-right" in lower or "narrative progression" in lower:
            base_cue = "последовательная история слева направо"
        elif "cinematic" in lower:
            base_cue = "кинематографичная постановка"
        elif "editorial" in lower:
            base_cue = "чистая редакционная композиция"
        elif "focused" in lower or "uncluttered" in lower:
            base_cue = "минимум лишнего, сильный фокус на главном"
        elif "semantic readability" in lower or "one glance" in lower:
            base_cue = "максимально ясная сюжетная композиция"
        else:
            base_cue = " ".join(base_value.split())[:100].rstrip(" ,;:.")
        if supplement:
            # Owner-authored refinement outranks generic art-direction style when
            # Alice's provider prompt must fit the official 500-character ceiling.
            return (
                "Уточнение пользователя: "
                + " ".join(supplement.split())[:150].rstrip(" ,;:.")
                + "; "
                + base_cue
            )
        return base_cue
    return ""


def _compiled_scene_direction_full(lines: tuple[str, ...]) -> str:
    """Return the complete selected art direction for roomy Responses input.

    The direct Yandex image endpoint is limited to 500 characters, so
    _compiled_scene_direction_cue intentionally compresses the selected direction.
    Yandex Responses has a much larger input budget and must receive the actual
    Art Director staging details instead of that lossy summary.
    """

    for line in lines:
        if not line.casefold().startswith("selected presentation direction"):
            continue
        value = line.split(":", 1)[-1].strip()
        base_value = value
        supplement = ""
        marker = ". Owner refinement: "
        if marker in value:
            base_value, raw_supplement = value.split(marker, 1)
            supplement = raw_supplement.split(
                ". Apply this only where compatible",
                1,
            )[0].strip(" .")
        base_value = " ".join(base_value.split()).strip(" .")[:1400]
        supplement = " ".join(supplement.split()).strip(" .")[:600]
        if supplement:
            return (
                "Режиссёрская постановка — соблюсти полностью: "
                + base_value
                + ". Уточнение пользователя: "
                + supplement
                + "."
            )
        if base_value:
            return "Режиссёрская постановка — соблюсти полностью: " + base_value + "."
    return ""


def _scene_contract_yandex_parts(
    brief: CreativeBrief,
) -> tuple[str, tuple[str, ...]]:
    value = brief.scene_contract
    if not isinstance(value, dict) or value.get("version") != 1:
        return "", ()

    topology = str(value.get("topology") or "").strip().lower()
    subject = " ".join(str(value.get("primary_subject") or "").split()).strip()[:120]

    def items(name: str, *, limit: int = 3) -> tuple[str, ...]:
        raw = value.get(name)
        if not isinstance(raw, list):
            return ()
        out: list[str] = []
        for item in raw:
            token = " ".join(str(item or "").split()).strip()
            if token and token not in out:
                out.append(token[:120])
            if len(out) >= limit:
                break
        return tuple(out)

    opening = items("initial_state")
    actions = items("actions")
    transition = items("transition")
    final = items("final_state")
    explicit_text = items("explicit_text")
    evidence = items("required_evidence", limit=2)
    cause = " ".join(str(value.get("cause") or "").split()).strip()[:120]
    cues: list[str] = []

    if topology == "transformation":
        # Layout (one scene vs storyboard) is decided from the compiled prompt.
        # The contract only contributes owner-language meaning. English evidence
        # ids such as "listening" or "visible progressive change" are not drawable
        # and previously replaced the owner's sentence.
        spoken: list[str] = []
        for token in (*opening, *actions, cause, *transition, *final):
            if token and _owner_language(token) and token not in spoken:
                spoken.append(token)
        if spoken:
            cues.append("Смыслы кадра: " + ", ".join(spoken) + ".")
    elif topology == "replacement":
        cues.append(
            "Покажи замену в том же окружении: исходный объект, само событие замены "
            "и физически правдоподобный результат."
        )
    elif topology == "sequence":
        detail = ", ".join(actions) or "запрошенные действия"
        cues.append("Последовательность ясно читается по порядку: " + detail + ".")
    elif topology == "comparison":
        cues.append("Сравнение двух запрошенных состояний/объектов читается сразу.")
    elif topology == "action":
        detail = ", ".join(actions)
        if detail:
            cues.append("Главное действие явно видно: " + detail + ".")

    if explicit_text:
        quoted = " / ".join(f"«{item}»" for item in explicit_text)
        cues.append("Точный запрошенный текст в кадре: " + quoted + ".")
    spoken_evidence = [item for item in evidence if _owner_language(item)]
    if spoken_evidence:
        cues.append("Обязательно видно: " + ", ".join(spoken_evidence) + ".")
    return subject, tuple(cues)


def _owner_language(token: str) -> bool:
    """True when a contract span is owner wording, not an English control id."""

    return bool(re.search(r"[А-Яа-яЁё]", str(token or "")))


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


def _compiled_style_cues(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Compress every explicit owner style choice into provider-visible tokens.

    Alice has a 500-character prompt ceiling. Raw compiler style sentences are too
    verbose and used to disappear behind semantic/safety content. This compact
    representation keeps additive quick-style buttons and explicit dimensions alive.
    """

    directives = tuple(line.casefold() for line in _compiled_style_directives(lines))
    mapping = (
        ("warm, welcoming and approachable", "тёплый дружелюбный"),
        ("soft, calm and reassuring", "мягкий спокойный"),
        ("bright, energetic and lively", "яркий энергичный"),
        ("refined premium feel", "премиальный"),
        ("cinematic, story-driven", "кинематографичный"),
        ("artistic, crafted", "художественный"),
        ("natural photographic feel", "натуральное фото"),
        ("warm color temperature", "тёплая гамма"),
        ("neutral color temperature", "нейтральная гамма"),
        ("cool color temperature", "холодная гамма"),
        ("friendly and approachable", "доброжелательное настроение"),
        ("calm and reassuring", "спокойное настроение"),
        ("bold and assertive", "дерзкое настроение"),
        ("dramatic and cinematic", "драматичное настроение"),
        ("aggressive and forceful", "агрессивное настроение"),
        ("playful and lively", "игривое настроение"),
        ("premium, restrained and status-oriented", "статусное настроение"),
        ("visual energy low", "низкая динамика"),
        ("medium visual energy", "средняя динамика"),
        ("high visual energy", "высокая динамика"),
        ("photorealistic visual language", "фотореализм"),
        ("credible realistic visual language", "реалистично"),
        ("semi-stylized but believable", "полустилизация"),
        ("illustrative artistic visual language", "иллюстрация"),
        ("bright, open lighting", "светлый свет"),
        ("balanced natural lighting", "естественный свет"),
        ("dark, moody lighting", "тёмный свет"),
        ("soft contrast", "мягкий контраст"),
        ("balanced contrast", "сбалансированный контраст"),
        ("strong visual contrast", "сильный контраст"),
        ("visually minimal and uncluttered", "минимум деталей"),
        ("balanced amount of visual detail", "сбалансированные детали"),
        ("rich but coherent visual detail", "много уместных деталей"),
        ("close-up composition", "крупный план"),
        ("medium-shot composition", "средний план"),
        ("wide composition", "общий план"),
        ("narrative story-scene composition", "сюжетная сцена"),
        ("explicit before/after composition", "до/после"),
        ("continuous transformation-focused composition", "композиция превращения"),
        ("motion static and composed", "статичное движение"),
        ("gentle, smooth motion", "плавное движение"),
        ("dynamic motion", "динамичное движение"),
        ("commercial presentation natural", "натуральная подача"),
        ("commercial presentation friendly", "дружелюбная подача"),
        ("commercial presentation professional", "профессиональная подача"),
        ("commercial presentation premium", "премиальная подача"),
        ("promotional advertising presentation", "рекламная подача"),
        ("do not reserve empty copy space", "без пустого места под текст"),
        ("small intentional area for later typography", "немного места под текст"),
        ("balanced intentional area for later typography", "место под текст"),
        ("large intentional area for later typography", "много места под текст"),
    )
    selected: list[str] = []
    for needle, label in mapping:
        if any(needle in line for line in directives):
            selected.append(label)
    if not selected:
        return ()
    return ("Стиль: " + "; ".join(dict.fromkeys(selected)) + ".",)


_OWNER_STYLE_FRAGMENT_RE = re.compile(
    r"(?:"
    r"\bв\s+стиле\s+[^,.;!?]{1,90}|"
    r"\bстил\w*(?:\s+изображен\w*)?\s*(?:[:—-]|на)\s*[^,.;!?]{1,90}|"
    r"\bin\s+(?:the\s+)?style\s+of\s+[^,.;!?]{1,90}|"
    r"\bstyle\s*[:—-]\s*[^,.;!?]{1,90}"
    r")",
    re.IGNORECASE,
)


def _owner_style_fragment(owner_request: str) -> str:
    match = _OWNER_STYLE_FRAGMENT_RE.search(str(owner_request or ""))
    if not match:
        return ""
    return " ".join(match.group(0).split()).strip()[:120]


_TRANSFORMATION_BECOMES_RE = re.compile(
    r"(?:\bстанов\w*|\bпревращ\w*\s+в\b|\bbecomes?\b|"
    r"\bturns?\s+into\b|\btransforms?\s+into\b)\s+"
    r"([^.!?;]{1,180})",
    re.IGNORECASE,
)
_TRANSFORMATION_FROM_TO_PATTERNS = (
    re.compile(
        r"\b(?:меня\w*|изменя\w*)\b[^.!?;]{0,60}?\bиз\s+"
        r"([^.!?;]{1,100}?)\s+\bв\s+([^.!?;]{1,120})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:меня\w*|изменя\w*)\b[^.!?;]{0,60}?\bс\s+"
        r"([^.!?;]{1,100}?)\s+\bна\s+([^.!?;]{1,120})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bchanges?\s+from\s+([^.!?;]{1,100}?)\s+to\s+"
        r"([^.!?;]{1,120})",
        re.IGNORECASE,
    ),
)
_EXPLICIT_STAGE_PATTERNS = (
    re.compile(
        r"\b(?:сначала|вначале|first)\b\s*[:,—-]?\s*([^;]{1,320})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:затем|потом|then)\b\s*[:,—-]?\s*([^;]{1,320})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:в\s+финальн\w*(?:\s+стади\w*)?|в\s+конце|наконец|finally|at\s+the\s+end)"
        r"\b\s*[:,—-]?\s*([^;.!?]{1,320})",
        re.IGNORECASE,
    ),
)
_STATE_CONNECTOR_RE = re.compile(
    r"^(?:[\s,/]*(?:(?:и|and|also|очень|более|намного|явно|"
    r"гораздо|ещ[её]|very|more|much|clearly)\b[\s,/]*)*)$",
    re.IGNORECASE,
)

_COMPACT_EXPLICIT_STAGE_EVIDENCE = {
    "доброжелательный расслабленный взгляд": "доброжелательный взгляд",
    "явно пушистая объёмная фактура": "пушистая объёмная фактура",
    "напряжённая закрытая поза": "напряжённая поза",
    "настороженный взгляд": "настороженный взгляд",
    "расслабленная поза": "расслабленная поза",
    "фактура или форма заметно смягчается": "фактура/форма смягчается",
    "спокойный расслабленный взгляд": "спокойный взгляд",
    "напряжённый взгляд и жёсткая поза": "сердитый взгляд, жёсткая поза",
    "тревожный взгляд и заметное напряжение тела": "тревожный взгляд, напряжённая поза",
    "радостное выражение, открытая поза": "радостный взгляд, открытая поза",
    "опущенный взгляд и сдержанная закрытая поза": "опущенный взгляд, закрытая поза",
    "явно колючая жёсткая фактура": "колючая жёсткая фактура",
    "фактура визуально мягче": "фактура мягче",
}


_STATE_EVIDENCE_RULES = (
    (
        re.compile(r"(?:\bдобр\w*|\bkind\b|\bgentle\b)", re.IGNORECASE),
        "доброжелательный расслабленный взгляд",
    ),
    (
        re.compile(r"(?:\bнапряж\w*|\btense\b)", re.IGNORECASE),
        "напряжённая закрытая поза",
    ),
    (
        re.compile(r"(?:\bнасторож\w*|\bwary\b|\bguarded\b)", re.IGNORECASE),
        "настороженный взгляд",
    ),
    (
        re.compile(r"(?:\bрасслаб\w*|\brelax\w*)", re.IGNORECASE),
        "расслабленная поза",
    ),
    (
        re.compile(r"(?:\bсмягч\w*|\bsoften\w*)", re.IGNORECASE),
        "фактура или форма заметно смягчается",
    ),
    (
        re.compile(r"(?:\bпушист\w*|\bfluffy\b)", re.IGNORECASE),
        "явно пушистая объёмная фактура",
    ),
    (
        re.compile(r"(?:\bмягк\w*|\bsoft\b)", re.IGNORECASE),
        "фактура визуально мягче",
    ),
    (
        re.compile(r"(?:\bспокойн\w*|\bcalm\b)", re.IGNORECASE),
        "спокойный расслабленный взгляд",
    ),
    (
        re.compile(r"(?:\bзл\w*|\bangry\b)", re.IGNORECASE),
        "напряжённый взгляд и жёсткая поза",
    ),
    (
        re.compile(r"(?:\bтревож\w*|\bиспуган\w*|\banxious\b|\bafraid\b)", re.IGNORECASE),
        "тревожный взгляд и заметное напряжение тела",
    ),
    (
        re.compile(r"(?:\bсчастлив\w*|\bhappy\b)", re.IGNORECASE),
        "радостное выражение, открытая поза",
    ),
    (
        re.compile(r"(?:\bгруст\w*|\bsad\b)", re.IGNORECASE),
        "опущенный взгляд и сдержанная закрытая поза",
    ),
    (
        re.compile(r"(?:\bколюч\w*|\bprickly\b)", re.IGNORECASE),
        "явно колючая жёсткая фактура",
    ),
    (
        re.compile(r"(?:\bгрязн\w*|\bdirty\b)", re.IGNORECASE),
        "видимые грязь, пятна или налёт",
    ),
    (
        re.compile(r"(?:\bчист\w*|\bclean\b)", re.IGNORECASE),
        "явно чистая поверхность или внешний вид",
    ),
    (
        re.compile(r"(?:\bблестящ\w*|\bshiny\b)", re.IGNORECASE),
        "чистый блеск и правдоподобные световые блики",
    ),
    (
        re.compile(r"(?:\bуверенн\w*|\bconfident\b)", re.IGNORECASE),
        "устойчивая открытая поза и уверенный взгляд",
    ),
)


def _state_matches(text: str) -> list[tuple[int, int, str]]:
    value = str(text or "")
    matches: list[tuple[int, int, str]] = []
    for pattern, evidence in _STATE_EVIDENCE_RULES:
        for match in pattern.finditer(value):
            matches.append((match.start(), match.end(), evidence))
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches


def _leading_state_evidence(text: str, *, limit: int = 3) -> tuple[str, ...]:
    value = str(text or "")
    matches = _state_matches(value)
    if not matches:
        return ()

    selected: list[str] = []
    cursor = 0
    for start, end, evidence in matches:
        gap = value[cursor:start]
        if not _STATE_CONNECTOR_RE.fullmatch(gap):
            if selected:
                break
            return ()
        selected.append(evidence)
        cursor = end
        if len(selected) >= limit:
            break
    return tuple(dict.fromkeys(selected))


def _trailing_state_evidence(text: str, *, limit: int = 2) -> tuple[str, ...]:
    value = str(text or "")
    matches = _state_matches(value)
    if not matches:
        return ()

    cluster: list[tuple[int, int, str]] = [matches[-1]]
    for item in reversed(matches[:-1]):
        next_item = cluster[-1]
        gap = value[item[1] : next_item[0]]
        if not _STATE_CONNECTOR_RE.fullmatch(gap):
            break
        cluster.append(item)
        if len(cluster) >= limit:
            break
    cluster.reverse()
    return tuple(dict.fromkeys(item[2] for item in cluster))


def _state_evidence_anywhere(text: str, *, limit: int = 3) -> tuple[str, ...]:
    """Collect ordered concrete state evidence from an explicitly scoped stage."""

    selected: list[str] = []
    for _start, _end, evidence in _state_matches(text):
        if evidence not in selected:
            selected.append(evidence)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _parsed_explicit_stage_evidence(
    owner_request: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Parse owner-authored сначала → затем → финал wording without copying labels.

    The stage markers provide the scope, so state words from later secondary
    subjects cannot leak into the transformed subject's earlier/final evidence.
    """

    request = " ".join(str(owner_request or "").split()).strip()
    matches = tuple(pattern.search(request) for pattern in _EXPLICIT_STAGE_PATTERNS)
    if any(match is None for match in matches):
        return None
    initial_match, middle_match, final_match = matches
    assert initial_match is not None
    assert middle_match is not None
    assert final_match is not None
    initial = _state_evidence_anywhere(initial_match.group(1), limit=3)
    middle = _state_evidence_anywhere(middle_match.group(1), limit=3)
    final = _state_evidence_anywhere(final_match.group(1), limit=3)
    if not final:
        return None
    return initial, middle, final


def _parsed_transformation_evidence(
    owner_request: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    request = " ".join(str(owner_request or "").split()).strip()

    for pattern in _TRANSFORMATION_FROM_TO_PATTERNS:
        match = pattern.search(request)
        if not match:
            continue
        initial = _leading_state_evidence(match.group(1), limit=2)
        final = _leading_state_evidence(match.group(2), limit=3)
        if final:
            return initial, final
        return None

    match = _TRANSFORMATION_BECOMES_RE.search(request)
    if not match:
        return None
    final = _leading_state_evidence(match.group(1), limit=3)
    if not final:
        return None
    initial = _trailing_state_evidence(request[: match.start()], limit=2)
    return initial, final


def _detailed_transformation_stage_cue(
    owner_request: str,
    *,
    listening: bool,
    allow_labels: bool,
) -> str | None:
    explicit_stages = _parsed_explicit_stage_evidence(owner_request)
    parsed = _parsed_transformation_evidence(owner_request)
    if explicit_stages is None and parsed is None:
        return None

    if explicit_stages is not None:
        initial_raw, middle_raw, final_raw = explicit_stages
        initial_evidence = tuple(
            _COMPACT_EXPLICIT_STAGE_EVIDENCE.get(item, item)
            for item in initial_raw
        )
        middle_evidence = tuple(
            _COMPACT_EXPLICIT_STAGE_EVIDENCE.get(item, item)
            for item in middle_raw
        )
        final_evidence = tuple(
            _COMPACT_EXPLICIT_STAGE_EVIDENCE.get(item, item)
            for item in final_raw
        )
    else:
        assert parsed is not None
        initial_evidence, final_evidence = parsed
        middle_evidence = ()

    opening = ", ".join(initial_evidence) if initial_evidence else "обычный"
    if listening:
        middle = "слушает аудио в заметных наушниках"
        if explicit_stages is None:
            middle += ", не символом волны"
        if middle_evidence:
            middle += ", " + ", ".join(middle_evidence)
        middle += ", и меняется"
    else:
        middle = (
            ", ".join(middle_evidence)
            if middle_evidence
            else "видна причина/действие и первые признаки изменения"
        )
    final = ", ".join(final_evidence)
    prefix = (
        "Один герой, три стадии: "
        if allow_labels
        else "Один герой, три стадии без подписей: "
    )
    return (
        prefix
        + "сначала "
        + opening
        + "; затем "
        + middle
        + "; финал — "
        + final
        + "."
    )

def _single_scene_transformation_cue(
    owner_request: str,
    *,
    listening: bool,
) -> str:
    """Keep arbitrary requested change visibly in-progress inside one subject."""

    parsed = _parsed_transformation_evidence(owner_request) if owner_request else None
    initial: tuple[str, ...] = ()
    final: tuple[str, ...] = ()
    if parsed is not None:
        initial, final = parsed

    details: list[str] = ["Одна сцена, один и тот же главный объект в процессе изменения"]
    if listening:
        details.append(
            "причина явно видна: субъект реально слушает аудио через заметные "
            "наушники, колонку или устройство, не символом волны"
        )

    if initial and final:
        details.append(
            "исходные признаки ещё частично видны: "
            + ", ".join(initial)
            + "; на том же объекте уже проявляются: "
            + ", ".join(final)
        )
    elif final:
        details.append(
            "запрошенный результат уже частично проявился: "
            + ", ".join(final)
            + ", но сам переход ещё визуально читается"
        )
    else:
        details.append(
            "сам переход виден на объекте, материале, фактуре, форме, позе или "
            "другом изменяемом признаке ровно так, как задано пользователем"
        )

    return (
        "; ".join(details)
        + ". Не своди запрос к готовому статичному финалу. "
        "Без копий главного объекта, панелей и триптиха, если это не просили."
    )


def _presentation_change_cue(*, transition: bool) -> str:
    if transition:
        return (
            "Одна сцена, тот же объект и тот же сюжет: меняется только визуальная "
            "подача. Переход запрошенного стиля, палитры, света, фона или другого "
            "параметра виден внутри композиции; не превращай его в физическую "
            "мутацию объекта и не дублируй объект."
        )
    return (
        "Сохрани объект, сюжет, геометрию и действия; измени только запрошенную "
        "визуальную подачу — стиль, палитру, фон, свет, композицию или иной указанный "
        "параметр. Без физической мутации и без до/после, если пользователь этого не просил."
    )


def _compiled_semantic_visual_cues(
    lines: tuple[str, ...],
    *,
    kind: str,
) -> tuple[str, ...]:
    """Translate compiler semantics into compact provider-visible scene cues.

    Alice AI ART has a hard prompt limit. Related semantic obligations are packed
    together so a transformation cannot crowd out its causal interaction (or vice
    versa) during compaction.
    """

    folded = tuple(line.casefold() for line in lines)
    cues: list[str] = []

    def has(prefix: str) -> bool:
        return any(line.startswith(prefix) for line in folded)

    transformation = has("the transformation is mandatory") or has(
        "the transformation is a mandatory"
    )
    presentation_transition = has(
        "the requested change is a visual-presentation transition"
    )
    presentation_change = presentation_transition or has(
        "the owner is editing visual presentation"
    ) or has("apply the requested presentation/style edit")
    storyboard = has("compact visual storyboard") or has("transformation stage detail")
    detailed_stages = has("transformation stage detail")
    visible_state = has("visible-state translation")
    listening = has("if the subject is listening")
    explicit_text = has("readable text is explicitly part")
    owner_request = _compiled_owner_request(lines)
    owner_style = _owner_style_fragment(owner_request)

    if presentation_change:
        cues.append(_presentation_change_cue(transition=presentation_transition))
    if owner_style:
        cues.append("Обязательный стиль пользователя: " + owner_style + ".")

    # Highest priority: one compact cue carries a subject/material state change and,
    # when present, its causal interaction. Presentation edits are handled separately
    # above so style words cannot be turned into anatomy or object mutation.
    if transformation and not storyboard and str(kind or "").strip().lower() != "video":
        cues.append(
            _single_scene_transformation_cue(
                owner_request,
                listening=listening,
            )
        )
    elif transformation:
        if str(kind or "").strip().lower() == "video":
            cues.append(
                "Тот же герой проходит видимое изменение: исходное состояние → "
                "причина/действие → ясный финал."
            )
        elif detailed_stages and owner_request:
            detailed_cue = _detailed_transformation_stage_cue(
                owner_request,
                listening=listening,
                allow_labels=explicit_text,
            )
            if detailed_cue:
                cues.append(detailed_cue)
            else:
                fallback = (
                    "Покажи изменение одного и того же героя в ясно различимых этапах; "
                    "исходное и итоговое состояния бери только из запроса, не додумывай "
                    "противоположность."
                )
                if listening:
                    fallback += (
                        " Причина видима: герой реально слушает аудио через наушники, "
                        "колонку или устройство."
                    )
                cues.append(fallback)
        elif listening:
            prefix = (
                "Один герой, три стадии: "
                if explicit_text
                else "Один герой, три стадии без подписей: "
            )
            final = (
                "финал заметно отличается по всем указанным признакам."
                if visible_state
                else "финал ясно отличается от начала."
            )
            cues.append(
                prefix
                + "сначала исходное состояние; затем герой явно слушает аудио "
                "через наушники/устройство и меняется; "
                + final
            )
        else:
            prefix = (
                "Один герой, три стадии: "
                if explicit_text
                else "Один герой, три стадии без подписей: "
            )
            final = (
                "финал заметно отличается по всем указанным признакам."
                if visible_state
                else "финал ясно отличается от начала."
            )
            cues.append(
                prefix
                + "сначала исходное состояние; затем видна причина/действие "
                "и изменение; "
                + final
            )
    elif visible_state:
        cues.append(
            "Запрошенное состояние явно читается по выражению, позе, фактуре/шерсти "
            "или материальному состоянию."
        )

    if has("treat object replacement as a constrained") or has(
        "object replacement is the core event"
    ):
        cues.append(
            "Замена видна: монтаж или до/после в том же окружении, не одиночный "
            "предмет. Результат физически правдоподобен; для монтажа видны крепления, "
            "управление и подключения."
        )

    # Do not duplicate listening when it is already packed into a transformation.
    if listening and not transformation:
        cues.append("Слушает аудио: видны наушники, колонка или устройство.")
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

    if not transformation and (
        has("respect the requested chronology") or has("the request contains a sequence")
    ):
        cues.append("Причинно-следственная последовательность действий ясно читается.")

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
        parts.append(
            "Неизвестные названия услуг, методов или брендов — только смысловой "
            "контекст. Не печатай эти названия в кадре и не превращай их в логотип."
        )
    return parts


def _bounded_yandex_prompt(
    *,
    scene_head: str,
    semantic_cues: tuple[str, ...],
    style_cues: tuple[str, ...],
    brief: CreativeBrief,
    extras: tuple[str, ...] = (),
) -> str:
    """Preserve owner meaning and style first under Alice's hard 500-char limit."""

    safety = _yandex_safety_parts(brief)
    stage_priority = any(
        cue.startswith("Один герой, три стадии")
        or cue.startswith("Три сцены")
        or cue.startswith("Один и тот же главный объект, три стадии")
        or cue.startswith("Одна сцена, один и тот же главный объект")
        or cue.startswith("Одна сцена, тот же объект")
        or cue.startswith("Сохрани объект, сюжет")
        or cue.startswith("Обязательный стиль пользователя")
        for cue in semantic_cues
    )
    normalized_scene_head = " ".join(str(scene_head or "").split()).strip()

    if stage_priority and len(normalized_scene_head) <= 200:
        # A short owner sentence is the scene. Do not spend the 500-character
        # budget on cue/safety reserves and then clip "становится добрым".
        style_block = _bounded_join(list(style_cues), limit=125)
        safety_reserve = _bounded_join(list(safety), limit=160)
        fixed = (
            len(style_block)
            + len(safety_reserve)
            + (1 if style_block else 0)
            + (1 if safety_reserve else 0)
        )
        bounded_scene_head = normalized_scene_head
        semantic_limit = max(
            80,
            _YANDEX_PROMPT_LIMIT - len(bounded_scene_head) - fixed - 1,
        )
        bounded_semantics = _bounded_join(list(semantic_cues), limit=semantic_limit)
        core = [part for part in (bounded_scene_head, bounded_semantics, style_block) if part]
        used = sum(len(part) for part in core) + max(0, len(core) - 1)
        remaining = max(0, _YANDEX_PROMPT_LIMIT - used - (1 if safety else 0))
        safety_block = _bounded_join(list(safety), limit=remaining)
        return _bounded_join(
            [*core, safety_block, *extras],
            limit=_YANDEX_PROMPT_LIMIT,
        )

    if stage_priority:
        # Long storyboard requests still protect stage evidence first. The owner
        # sentence is clipped only after the stages, style and safety reserves.
        bounded_semantics = _bounded_join(list(semantic_cues), limit=300)
        style_block = _bounded_join(list(style_cues), limit=125)
        # Reserve the whole compact safety block when it fits. Only in an
        # impossible all-at-once 500-char case may the ordered tail be trimmed.
        safety_reserve = _bounded_join(list(safety), limit=180)
        reserved = (
            len(bounded_semantics)
            + len(style_block)
            + len(safety_reserve)
            + sum(
                1
                for block in (bounded_semantics, style_block, safety_reserve)
                if block
            )
        )
        scene_limit = max(40, _YANDEX_PROMPT_LIMIT - reserved)
        if len(normalized_scene_head) <= scene_limit:
            bounded_scene_head = normalized_scene_head
        else:
            # _bounded_join intentionally refuses tiny fragments. Here even a
            # short natural subject anchor is semantically valuable (for example
            # keeping "ёж" instead of leaving only generic "one hero" cues).
            bounded_scene_head = (
                normalized_scene_head[:scene_limit]
                .rsplit(" ", 1)[0]
                .rstrip(" ,;:.")
            )
            if not bounded_scene_head:
                bounded_scene_head = normalized_scene_head[:scene_limit].rstrip()
        core = [bounded_scene_head, bounded_semantics, style_block]
        core = [part for part in core if part]
        used = sum(len(part) for part in core) + max(0, len(core) - 1)
        remaining = max(0, _YANDEX_PROMPT_LIMIT - used - (1 if safety else 0))
        safety_block = _bounded_join(list(safety), limit=remaining)
        return _bounded_join(
            [*core, safety_block, *extras],
            limit=_YANDEX_PROMPT_LIMIT,
        )

    safety_block = _bounded_join(list(safety), limit=_YANDEX_PROMPT_LIMIT)
    style_block = _bounded_join(list(style_cues), limit=120)
    minimum_scene_head = 180
    scene_reserve = min(len(normalized_scene_head), minimum_scene_head)
    fixed_reserved = (
        len(safety_block)
        + len(style_block)
        + (1 if safety_block else 0)
        + (1 if style_block else 0)
    )
    semantic_budget = max(
        70,
        _YANDEX_PROMPT_LIMIT
        - fixed_reserved
        - scene_reserve
        - 1,
    )
    bounded_semantics = _bounded_join(list(semantic_cues), limit=semantic_budget)
    semantic_parts = tuple(
        line for line in bounded_semantics.splitlines() if line.strip()
    )
    style_parts = tuple(line for line in style_block.splitlines() if line.strip())
    reserved = (
        fixed_reserved
        + len(bounded_semantics)
        + (1 if bounded_semantics else 0)
    )
    scene_limit = max(scene_reserve, _YANDEX_PROMPT_LIMIT - reserved)
    bounded_scene_head = _bounded_join([normalized_scene_head], limit=scene_limit)
    return _bounded_join(
        [bounded_scene_head, *semantic_parts, *style_parts, *safety, *extras],
        limit=_YANDEX_PROMPT_LIMIT,
    )

def _adapt_yandex(brief: CreativeBrief) -> CreativeBrief:
    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)

    metadata = dict(brief.metadata or {})
    if not owner_request:
        responses_input = _bounded_join(
            _yandex_natural_prompt_parts(brief),
            limit=4000,
        )
        if responses_input:
            metadata["yandex_responses_input"] = responses_input
        prompt = _bounded_join([brief.prompt], limit=_YANDEX_PROMPT_LIMIT)
        return replace(brief, prompt=prompt, metadata=metadata)

    extras: list[str] = []
    contract_head, contract_cues = _scene_contract_yandex_parts(brief)
    semantic_cues = tuple(
        dict.fromkeys(
            (*contract_cues, *_compiled_semantic_visual_cues(lines, kind=brief.kind))
        )
    )
    direction_cue = _compiled_scene_direction_cue(lines)
    full_direction = _compiled_scene_direction_full(lines)
    style_cues = tuple(
        dict.fromkeys(
            ((direction_cue,) if direction_cue else ())
            + _compiled_style_cues(lines)
        )
    )
    responses_style_cues = tuple(
        dict.fromkeys(
            ((full_direction,) if full_direction else ())
            + _compiled_style_cues(lines)
        )
    )

    # Responses has an LLM orchestrator in front of Alice AI ART. Feed it the same
    # immutable scene semantics that the direct 500-character path receives.
    # Previously this path got only the raw owner request + style/safety, which
    # bypassed transformation topology, causal action and identity continuity.
    # Keep the wording natural (no compiler meta-language) while preserving the
    # canonical scene contract and selected art direction.
    responses_scene = owner_request or contract_head
    responses_input = _bounded_join(
        [
            responses_scene,
            *semantic_cues,
            *responses_style_cues,
            *_natural_safety_parts(brief),
            *_natural_policy_parts(brief),
        ],
        limit=4000,
    )
    if responses_input:
        metadata["yandex_responses_input"] = responses_input
    folded = tuple(line.casefold() for line in lines)
    if any(
        line.startswith("treat object replacement as a constrained")
        for line in folded
    ):
        scene_head = (
            "Покажи именно событие замены, сохрани то же окружение. " + owner_request
        )
    else:
        # The owner's sentence is the scene. A contract subject such as "Ёж"
        # must not replace it: Alice then draws a generic portrait and ignores
        # the action and the change.
        scene_head = owner_request
    prompt = _bounded_yandex_prompt(
        scene_head=scene_head,
        semantic_cues=semantic_cues,
        style_cues=style_cues,
        brief=brief,
        extras=tuple(extras),
    )
    return replace(brief, prompt=prompt, metadata=metadata)


def _adapt_yandex_motion(brief: CreativeBrief) -> CreativeBrief:
    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)
    extras: list[str] = []
    semantic_cues: tuple[str, ...] = ()
    style_cues: tuple[str, ...] = ()
    if owner_request:
        scene = "Ключевой кадр для короткого вертикального видео: " + owner_request
        semantic_cues = _compiled_semantic_visual_cues(lines, kind="image")
        style_cues = _compiled_style_cues(lines)
        if str(brief.brand_context or "").strip():
            extras.append("Названия бренда, услуг и методов не печатать в кадре.")
    else:
        # Provider-direct/legacy requests are already natural scene descriptions.
        scene = (
            "Ключевой кадр для короткого вертикального видео: "
            + " ".join(str(brief.prompt or "").split())
        )
    prompt = _bounded_yandex_prompt(
        scene_head=scene,
        semantic_cues=semantic_cues,
        style_cues=style_cues,
        brief=brief,
        extras=tuple(extras),
    )
    return replace(brief, prompt=prompt)


def _bounded_gigachat_prompt(
    *,
    scene_head: str,
    semantic_cues: tuple[str, ...],
    style_cues: tuple[str, ...],
    brief: CreativeBrief,
) -> str:
    """Keep meaning and mandatory safety even for a near-limit owner request."""

    safety = (*_natural_safety_parts(brief), *_natural_policy_parts(brief))
    safety_block = _bounded_join(list(safety), limit=520)
    semantic_block = _bounded_join(list(semantic_cues), limit=520)
    style_block = _bounded_join(list(style_cues), limit=180)

    reserved_blocks = [block for block in (semantic_block, style_block, safety_block) if block]
    reserved = sum(len(block) for block in reserved_blocks) + len(reserved_blocks)
    scene_limit = max(420, _GIGACHAT_PROMPT_LIMIT - reserved)
    bounded_scene = _bounded_join([scene_head], limit=scene_limit)

    return _bounded_join(
        [
            bounded_scene,
            semantic_block,
            style_block,
            safety_block,
        ],
        limit=_GIGACHAT_PROMPT_LIMIT,
    )


def _adapt_gigachat(brief: CreativeBrief) -> CreativeBrief:
    """Send GigaChat a natural scene description instead of compiler meta-language.

    GigaChat is the default RU image fallback. It accepts a substantially larger
    prompt than Alice AI ART, so keep the owner's wording plus semantic, style and
    safety cues while still removing internal contract phrases such as "mandatory"
    and raw business grounding that can become accidental lettering in the image.
    """

    lines = _compiled_directives(brief.prompt)
    owner_request = _compiled_owner_request(lines)
    if not owner_request:
        return replace(
            brief,
            prompt=_bounded_join([brief.prompt], limit=_GIGACHAT_PROMPT_LIMIT),
        )

    semantic_cues = _compiled_semantic_visual_cues(lines, kind=brief.kind)
    style_cues = _compiled_style_cues(lines)
    folded = tuple(line.casefold() for line in lines)
    if any(line.startswith("the transformation is mandatory") for line in folded):
        scene_head = owner_request
    elif any(
        line.startswith("treat object replacement as a constrained")
        for line in folded
    ):
        scene_head = (
            "Покажи именно событие замены, сохрани то же окружение. " + owner_request
        )
    else:
        scene_head = owner_request

    prompt = _bounded_gigachat_prompt(
        scene_head=scene_head,
        semantic_cues=semantic_cues,
        style_cues=style_cues,
        brief=brief,
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
    if name == "gigachat":
        return _adapt_gigachat(value)
    if name == "runway":
        return _adapt_runway(value)
    # OpenAI and self-hosted gateways receive the frozen compiled prompt unchanged.
    # Provider adapters must not rewrite semantics without a concrete provider
    # contract that requires it.
    return value


__all__ = ["PROMPT_ADAPTER_VERSION", "adapt_visual_brief_for_provider"]
