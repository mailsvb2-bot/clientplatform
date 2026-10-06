from __future__ import annotations

"""Provider-neutral visual scene contract.

The contract is the canonical meaning representation between an owner's natural
language request, provider prompt adapters and post-generation vision QA.  It is
intentionally small, bounded and serializable so provider-specific prompt limits
cannot become the source of truth for the scene.
"""

from dataclasses import dataclass
import re

SCENE_CONTRACT_VERSION = 1
SCENE_TOPOLOGIES = frozenset(
    {
        "static",
        "action",
        "transformation",
        "sequence",
        "comparison",
        "replacement",
    }
)
_MAX_FIELD = 240
_MAX_ITEMS = 8


def _clean(value: object, *, limit: int = _MAX_FIELD) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValueError("visual_scene_contract_invalid")
    return text


def _items(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("visual_scene_contract_invalid")
    if len(value) > _MAX_ITEMS:
        raise ValueError("visual_scene_contract_invalid")
    result: list[str] = []
    for item in value:
        text = _clean(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class VisualSceneContract:
    version: int
    topology: str
    primary_subject: str
    initial_state: tuple[str, ...]
    actions: tuple[str, ...]
    cause: str
    transition: tuple[str, ...]
    final_state: tuple[str, ...]
    explicit_text: tuple[str, ...]
    required_evidence: tuple[str, ...]
    forbidden: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": self.version,
            "topology": self.topology,
            "primary_subject": self.primary_subject,
            "initial_state": list(self.initial_state),
            "actions": list(self.actions),
            "cause": self.cause,
            "transition": list(self.transition),
            "final_state": list(self.final_state),
            "explicit_text": list(self.explicit_text),
            "required_evidence": list(self.required_evidence),
            "forbidden": list(self.forbidden),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "VisualSceneContract":
        required = {
            "version",
            "topology",
            "primary_subject",
            "initial_state",
            "actions",
            "cause",
            "transition",
            "final_state",
            "explicit_text",
            "required_evidence",
            "forbidden",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("visual_scene_contract_invalid")
        topology = _clean(value.get("topology"), limit=32).lower()
        if value.get("version") != SCENE_CONTRACT_VERSION or topology not in SCENE_TOPOLOGIES:
            raise ValueError("visual_scene_contract_invalid")
        return cls(
            version=SCENE_CONTRACT_VERSION,
            topology=topology,
            primary_subject=_clean(value.get("primary_subject"), limit=160),
            initial_state=_items(value.get("initial_state")),
            actions=_items(value.get("actions")),
            cause=_clean(value.get("cause")),
            transition=_items(value.get("transition")),
            final_state=_items(value.get("final_state")),
            explicit_text=_items(value.get("explicit_text")),
            required_evidence=_items(value.get("required_evidence")),
            forbidden=_items(value.get("forbidden")),
        )

    def compact_signature(self) -> str:
        parts = [
            self.primary_subject,
            *self.initial_state,
            *self.actions,
            self.cause,
            *self.transition,
            *self.final_state,
        ]
        return " | ".join(part for part in parts if part)


def _topology(flags: tuple[str, ...]) -> str:
    values = set(flags)
    if "object_replacement" in values:
        return "replacement"
    if "transformation" in values:
        return "transformation"
    if "comparison" in values:
        return "comparison"
    if "sequence" in values:
        return "sequence"
    if values.intersection(
        {
            "listening",
            "watching",
            "reading",
            "using",
            "holding",
            "eating_or_drinking",
            "generic_action",
        }
    ):
        return "action"
    return "static"


def fallback_scene_contract(
    *,
    request: str,
    semantic_flags: tuple[str, ...],
) -> VisualSceneContract:
    """Conservative deterministic fallback when semantic AI planning is unavailable.

    It never invents named entities or claims.  The full owner request remains the
    source of truth; these evidence requirements only protect known visual relations.
    """

    owner = " ".join(str(request or "").replace("\x00", " ").split()).strip()
    topology = _topology(semantic_flags)
    flags = set(semantic_flags)
    evidence: list[str] = []
    forbidden: list[str] = ["unrelated subject", "unrequested readable text"]
    actions: list[str] = []
    transition: list[str] = []

    if "listening" in flags:
        actions.append("listening")
        evidence.append("visible audio interaction")
    if "watching" in flags:
        actions.append("watching")
        evidence.append("visible gaze connected to viewed source")
    if "reading" in flags:
        actions.append("reading")
        evidence.append("visible readable-material interaction")
    if "using" in flags:
        actions.append("using requested object")
        evidence.append("physical interaction with requested object")
    if "holding" in flags:
        actions.append("holding requested object")
        evidence.append("requested object visibly held")
    if "eating_or_drinking" in flags:
        actions.append("eating or drinking")
        evidence.append("requested consumption action visible")
    if "transformation" in flags:
        transition.append("visible progressive change")
        if "storyboard" in flags:
            evidence.extend(
                [
                    "same subject identity across stages",
                    "opening state visible",
                    "causal action connected to change",
                    "requested final state visibly different",
                ]
            )
            forbidden.extend(
                [
                    "unrelated characters used as stages",
                    "single final-state portrait",
                    "stage labels or arrows unless explicitly requested",
                ]
            )
        else:
            evidence.extend(
                [
                    "subject shown once in one coherent scene",
                    "causal action visible together with the changed qualities",
                    "requested changed qualities visibly readable",
                ]
            )
            forbidden.extend(
                [
                    "identical repeated portraits of the same subject",
                    "triptych of the same portrait",
                    "stage labels or arrows unless explicitly requested",
                ]
            )
    if "sequence" in flags:
        evidence.append("requested chronology visible")
    if "object_replacement" in flags:
        evidence.extend(
            [
                "same environment across replacement",
                "replacement action or before/after relation visible",
            ]
        )
    if "explicit_text" in flags:
        forbidden = [item for item in forbidden if item != "unrequested readable text"]
        forbidden.append("readable text other than owner-requested wording")

    # Conservative subject anchor: when the request is shaped like
    # "ёж, который ...", keep the exact owner-authored noun phrase before the
    # relative/action clause instead of letting a provider see only generic "hero".
    # No new noun is invented; if no safe boundary exists we keep the bounded request.
    subject = owner[:160].rstrip(" ,;:.")
    subject_match = re.match(
        r"^(.{1,120}?)(?:,?\s+(?:котор(?:ый|ая|ое|ые)|who|which|that)\b)",
        owner,
        flags=re.IGNORECASE,
    )
    if subject_match:
        candidate = subject_match.group(1).strip(" ,;:.")
        if candidate:
            subject = candidate
    return VisualSceneContract(
        version=SCENE_CONTRACT_VERSION,
        topology=topology,
        primary_subject=subject,
        initial_state=(),
        actions=tuple(actions),
        cause="",
        transition=tuple(transition),
        final_state=(),
        explicit_text=(),
        required_evidence=tuple(dict.fromkeys(evidence)),
        forbidden=tuple(dict.fromkeys(forbidden)),
    )


def grounded_span(owner_request: str, candidate: object) -> bool:
    """Return true only when planner text is grounded in the owner's wording."""

    candidate_text = " ".join(str(candidate or "").split()).strip().casefold()
    if not candidate_text:
        return True
    owner = " ".join(str(owner_request or "").split()).strip().casefold()
    if candidate_text in owner:
        return True
    # Normalise punctuation only; do not perform semantic expansion or synonymy.
    def words(value: str) -> str:
        return " ".join(re.findall(r"[\wёЁ-]+", value, flags=re.UNICODE)).casefold()

    normalized_candidate = words(candidate_text)
    normalized_owner = words(owner)
    return bool(normalized_candidate and normalized_candidate in normalized_owner)


__all__ = [
    "SCENE_CONTRACT_VERSION",
    "SCENE_TOPOLOGIES",
    "VisualSceneContract",
    "fallback_scene_contract",
    "grounded_span",
]
