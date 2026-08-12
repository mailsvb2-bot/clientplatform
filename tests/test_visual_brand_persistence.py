from __future__ import annotations

import sqlite3

import pytest

from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.domain.visual_brand import TenantBrandDNA
from clientplatform.infrastructure.visual_brand_repository import VisualBrandRepository
from services.db.schema import clientplatform_activity, clientplatform_tenancy

BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
OWNER_MEMBER_ID = "22222222-2222-4222-8222-222222222222"
MARKETER_MEMBER_ID = "33333333-3333-4333-8333-333333333333"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO users(user_id) VALUES(?)", [(1,), (2,)])
    clientplatform_tenancy.ensure(conn)
    clientplatform_activity.ensure(conn)
    conn.execute(
        "INSERT INTO businesses(id, name, status, created_by_user_id, created_at, updated_at) "
        "VALUES(?, 'Практика Анны', 'active', 1, '2026-08-12', '2026-08-12')",
        (BUSINESS_ID,),
    )
    conn.executemany(
        """
        INSERT INTO business_members(
            id, business_id, user_id, role, status, created_at, updated_at, revoked_at
        ) VALUES(?, ?, ?, ?, 'active', '2026-08-12', '2026-08-12', NULL)
        """,
        [
            (OWNER_MEMBER_ID, BUSINESS_ID, 1, "owner"),
            (MARKETER_MEMBER_ID, BUSINESS_ID, 2, "marketer"),
        ],
    )
    conn.execute(
        """
        INSERT INTO business_profiles(
            business_id, activity_description, timezone, status,
            created_by_member_id, created_at, updated_at
        ) VALUES(?, 'Психологическая практика', 'Europe/Amsterdam', 'ready', ?,
                 '2026-08-12', '2026-08-12')
        """,
        (BUSINESS_ID, OWNER_MEMBER_ID),
    )
    return conn


def _actor(user_id: int, member_id: str, role: PlatformRole) -> TenantContext:
    return TenantContext(
        business_id=BUSINESS_ID,
        user_id=user_id,
        membership_id=member_id,
        role=role,
    )


def test_visual_brand_defaults_to_business_name_and_is_persisted():
    conn = _db()
    repo = VisualBrandRepository(conn)
    owner = _actor(1, OWNER_MEMBER_ID, PlatformRole.OWNER)
    initial = repo.get(actor=owner)
    assert initial.display_name == "Практика Анны"
    assert initial.primary_color == "#172033"
    saved = repo.update(
        actor=owner,
        brand=TenantBrandDNA(
            business_id=BUSINESS_ID,
            display_name="Анна · спокойная практика",
            tone=("human", "calm"),
            visual_keywords=("natural light", "real office"),
            primary_color="#123456",
            accent_color="#ABCDEF",
            text_color="#FFFFFF",
        ),
        now="2026-08-12T08:00:00+00:00",
    )
    assert saved.display_name == "Анна · спокойная практика"
    assert saved.tone == ("human", "calm")
    assert saved.visual_keywords == ("natural light", "real office")
    row = conn.execute(
        "SELECT brand_tone_json, brand_updated_at FROM business_profiles WHERE business_id=?",
        (BUSINESS_ID,),
    ).fetchone()
    assert row["brand_tone_json"] == '["human","calm"]'
    assert row["brand_updated_at"] == "2026-08-12T08:00:00+00:00"


def test_visual_brand_write_preserves_business_management_boundary():
    conn = _db()
    repo = VisualBrandRepository(conn)
    marketer = _actor(2, MARKETER_MEMBER_ID, PlatformRole.MARKETER)
    with pytest.raises(TenantPermissionDenied):
        repo.update(
            actor=marketer,
            brand=TenantBrandDNA(business_id=BUSINESS_ID, display_name="Forbidden"),
        )


def test_visual_brand_fingerprint_is_normalized_and_semantic():
    first = TenantBrandDNA(
        business_id=BUSINESS_ID,
        display_name="  Brand   Name ",
        tone=("Human", "Clear"),
        primary_color="#abcdef",
    )
    same = TenantBrandDNA(
        business_id=BUSINESS_ID,
        display_name="Brand Name",
        tone=("Human", "Clear"),
        primary_color="#ABCDEF",
    )
    changed = TenantBrandDNA(
        business_id=BUSINESS_ID,
        display_name="Brand Name",
        tone=("Bold",),
        primary_color="#ABCDEF",
    )
    assert first.fingerprint() == same.fingerprint()
    assert first.fingerprint() != changed.fingerprint()


def test_activity_schema_adds_brand_columns_to_legacy_profile_table():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE business_profiles(
            business_id TEXT PRIMARY KEY,
            activity_description TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    clientplatform_activity.ensure(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(business_profiles)")}
    assert {
        "brand_display_name", "brand_tone_json", "brand_visual_keywords_json",
        "brand_forbidden_visuals_json", "brand_primary_color", "brand_accent_color",
        "brand_text_color", "brand_updated_at",
    }.issubset(columns)
