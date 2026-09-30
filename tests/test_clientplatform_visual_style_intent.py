from __future__ import annotations

import json
import sqlite3

from clientplatform.application import visual_creatives
from clientplatform.domain.visual_prompt_compiler import compile_visual_prompt
from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    infer_visual_style,
    merge_visual_styles,
    style_missing_fields,
    visual_style_from_json,
    visual_style_preset,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.visual_style_preference_repository import (
    VisualStylePreferenceRepository,
)
from services.db.schema import clientplatform_creative_experiments, clientplatform_tenancy


def _actor_fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    clientplatform_tenancy.ensure(conn)
    clientplatform_creative_experiments.ensure(conn)
    tenancy = TenancyRepository(conn)
    access = tenancy.create_business(owner_user_id=101, name="Style Business")
    actor = tenancy.resolve_context(user_id=101, business_id=access.business.id)
    return conn, actor


def test_short_natural_language_infers_style_without_prompt_engineering() -> None:
    style = infer_visual_style(
        "тёплая мягкая реалистичная картинка, спокойная и светлая"
    )
    assert style.color_temperature == "warm"
    assert style.emotional_tone == "calm"
    assert style.realism == "realistic"
    assert style.lighting == "bright"


def test_explicit_choice_overrides_request_then_saved_preference() -> None:
    explicit = VisualStyleIntent(color_temperature="cool", emotional_tone="dramatic")
    inferred = infer_visual_style("тёплая добрая фотография")
    saved = VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="friendly",
        realism="illustrative",
    )

    resolved = merge_visual_styles(
        explicit=explicit,
        inferred=inferred,
        saved=saved,
    )

    assert resolved.color_temperature == "cool"
    assert resolved.emotional_tone == "dramatic"
    assert resolved.realism == "realistic"


def test_adaptive_questions_skip_style_already_present_in_request() -> None:
    missing = style_missing_fields(
        request="тёплая спокойная реалистичная фотография",
        explicit=VisualStyleIntent(),
        saved=None,
    )

    assert "color_temperature" not in missing
    assert "emotional_tone" not in missing
    assert "realism" not in missing
    assert "composition" in missing


def test_presets_expand_into_structured_style_not_free_text() -> None:
    calm = visual_style_preset("soft_calm")
    premium = visual_style_preset("premium")

    assert calm.color_temperature == "warm"
    assert calm.energy == "low"
    assert calm.contrast == "soft"
    assert premium.emotional_tone == "premium"
    assert premium.realism == "photorealistic"


def test_style_intent_json_roundtrip_is_strict_and_deterministic() -> None:
    style = VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="friendly",
        realism="photorealistic",
        composition="story_scene",
    )
    first = style.to_json()
    second = style.to_json()

    assert first == second
    assert visual_style_from_json(first) == style.normalized()


def test_prompt_compiler_preserves_semantics_while_applying_style() -> None:
    request = (
        "колючий ёж, который слушает аудиосессию «Тишина» "
        "и превращается в доброго и мягкого"
    )
    style = VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="calm",
        realism="illustrative",
        composition="transformation",
    )

    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        brand_context="Brand name: Тишина.",
        style_intent=style,
    )

    assert "listening unmistakable" in compiled.prompt
    assert "transformation is mandatory visual evidence" in compiled.prompt
    assert "warm color palette" in compiled.prompt
    assert "calm and gentle mood" in compiled.prompt
    assert "illustrative artistic treatment" in compiled.prompt
    assert "continuous transformation composition" in compiled.prompt
    assert compiled.prompt.index("listening unmistakable") < compiled.prompt.index(
        "Visual style intent"
    )


def test_frozen_payload_v2_locks_semantics_style_and_compiler_versions() -> None:
    frozen = visual_creatives.freeze_business_image_payload(
        request=(
            "колючий ёж слушает аудиосессию «Тишина» "
            "и превращается в доброго и мягкого"
        ),
        brand_context="Brand name: Тишина.",
        country_code="RU",
        style_intent=VisualStyleIntent(
            color_temperature="warm",
            emotional_tone="friendly",
            composition="before_after",
        ),
    )
    payload = json.loads(frozen)
    snapshot = payload["intent_snapshot"]

    assert payload["version"] == 2
    assert snapshot["semantic_compiler_version"] >= 1
    assert snapshot["prompt_compiler_version"] >= 1
    assert snapshot["style_schema_version"] == 1
    assert "transformation" in snapshot["semantic_flags"]
    assert "listening" in snapshot["semantic_flags"]
    assert snapshot["style_intent"]["color_temperature"] == "warm"
    assert snapshot["style_intent"]["composition"] == "before_after"


def test_legacy_v1_frozen_payload_remains_readable() -> None:
    current = json.loads(
        visual_creatives.freeze_business_image_payload(
            request="спокойный кабинет психолога",
        )
    )
    current["version"] = 1
    current.pop("intent_snapshot")

    brief, wait_seconds = visual_creatives._load_frozen_business_image_payload(
        json.dumps(current, ensure_ascii=False)
    )

    assert brief.kind == "image"
    assert wait_seconds == 20


def test_style_preferences_are_member_and_business_scoped() -> None:
    conn, actor = _actor_fixture()
    try:
        repo = VisualStylePreferenceRepository(conn)
        assert repo.get(actor=actor).is_auto()

        saved = repo.save(
            actor=actor,
            style=VisualStyleIntent(
                color_temperature="warm",
                emotional_tone="calm",
                realism="realistic",
            ),
            now="2026-09-30T00:00:00+00:00",
        )

        assert saved.color_temperature == "warm"
        assert repo.get(actor=actor) == saved
        row = conn.execute(
            """
            SELECT business_id,member_id,style_json
            FROM visual_style_preferences
            WHERE business_id=? AND member_id=?
            """,
            (actor.business_id, actor.membership_id),
        ).fetchone()
        assert row is not None
        assert row["business_id"] == actor.business_id
        assert row["member_id"] == actor.membership_id
        assert "warm" in row["style_json"]
    finally:
        conn.close()


def test_style_preference_schema_contains_no_media_bytes_or_customer_identity() -> None:
    conn, _actor = _actor_fixture()
    try:
        columns = {
            str(row["name"]): str(row["type"]).upper()
            for row in conn.execute(
                "PRAGMA table_info(visual_style_preferences)"
            ).fetchall()
        }
        assert "style_json" in columns
        assert "customer_id" not in columns
        assert all("BLOB" not in declared for declared in columns.values())
    finally:
        conn.close()
