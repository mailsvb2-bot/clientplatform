from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.domain.ad_connections import (
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.domain.ad_spend import ProviderBudgetSnapshot
from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    stable_creative_id,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_spend_operation_repository import (
    AdSpendOperationRepository,
)
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.integrations.yandex_direct import YandexTokenBundle
from services.db import db
from services.db.runtime import CONFIG
from services.schema import init_db


_NOW = datetime(2026, 8, 6, 7, 30, tzinfo=timezone.utc)


def _seed_authorized_spend() -> tuple[object, str]:
    vault = InMemoryAdCredentialVault()
    with db() as conn:
        tenancy = TenancyRepository(conn)
        activity = ActivityRepository(conn)
        bookings = BookingRepository(conn)
        promotions = PromotionRepository(conn)
        ads = AdConnectionRepository(conn, vault=vault)
        spend = AdSpendRepository(conn)

        access = tenancy.create_business(
            owner_user_id=9_300_001_401,
            name=f"Ad concurrency {uuid.uuid4().hex[:10]}",
        )
        actor = tenancy.resolve_context(
            user_id=9_300_001_401,
            business_id=access.business.id,
        )
        activity.upsert_profile(
            actor=actor,
            activity_description="PostgreSQL advertising concurrency probe",
            timezone_name="Europe/Amsterdam",
            now=_NOW.isoformat(),
        )
        capability = activity.enable_capability(
            actor=actor,
            connector_key="services",
            now=_NOW.isoformat(),
        )
        offering = activity.create_offering(
            actor=actor,
            capability_id=capability.id,
            title="Concurrency service",
            description="Probe-only service",
            now=_NOW.isoformat(),
        )
        slot = bookings.create_slot(
            actor=actor,
            offering_id=offering.id,
            local_start="10.08.2026 12:00",
            duration_minutes=60,
            now=_NOW.isoformat(),
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id(
                f"ad-concurrency-{uuid.uuid4().hex}",
                "website",
            ),
            headline="Concurrency probe",
            primary_text="Concurrency probe advertisement",
            description="CI only",
        )
        promotion, _created = promotions.create_or_refresh_campaign(
            actor=actor,
            slot_id=slot.slot.id,
            channel=PromotionChannel.WEBSITE,
            creative=creative,
            now=_NOW.isoformat(),
        )

        state = new_oauth_state()
        verifier = new_pkce_verifier()
        ads.create_oauth_session(
            actor=actor,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
            now=_NOW,
        )
        session, _stored_verifier = ads.consume_oauth_session(
            state=state,
            now=_NOW,
        )
        connection = ads.activate_oauth_connection(
            session=session,
            external_account_id=f"93{uuid.uuid4().int % 10_000_000:07d}",
            external_login="postgres-ci-owner",
            token_bundle_json=YandexTokenBundle(
                access_token="ci-access",
                token_type="bearer",
                expires_in=3600,
                refresh_token="ci-refresh",
                scope=("direct:api",),
            ).to_json(),
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
            now=_NOW,
        )
        job = ads.create_or_get_job(
            actor=actor,
            promotion_campaign_id=promotion.id,
            connection_id=connection.id,
            external_campaign_id="9300001",
            external_campaign_name="PostgreSQL CI campaign",
            region_ids=(213,),
            source_url="https://example.test/postgres-ad-concurrency",
            title=promotion.creative.headline,
            text=promotion.creative.primary_text,
            creative_id=promotion.creative.creative_id,
            now=_NOW,
        )
        ads.queue_job(actor=actor, job_id=job.id, now=_NOW)
        claimed = ads.claim_due_job(now=_NOW)
        if claimed is None:
            raise AssertionError("provider publication job was not claimable")
        claimed_job, lock_token = claimed
        submitted = ads.complete_job(
            job=claimed_job,
            lock_token=lock_token,
            external_ad_group_id="9300101",
            external_ad_id="9300201",
            now=_NOW,
        )
        snapshot = ProviderBudgetSnapshot(
            provider=AdProvider.YANDEX_DIRECT,
            connection_id=connection.id,
            external_account_id=connection.external_account_id,
            external_campaign_id=submitted.external_campaign_id,
            currency="RUB",
            available_budget_minor=100_000,
            spent_today_minor=0,
            campaign_status="ON",
            strategy="HIGHEST_POSITION",
            launch_eligible=True,
            provider_version="postgres-ci-v1",
            captured_at=_NOW,
            valid_until=_NOW + timedelta(minutes=15),
        )
        authorization = spend.create_or_get_draft(
            actor=actor,
            publication_job_id=submitted.id,
            snapshot=snapshot,
            region_ids=(213,),
            hard_cap_minor=10_000,
            daily_cap_minor=2_000,
            authorization_expires_at=_NOW + timedelta(minutes=10),
            now=_NOW,
        )
        spend.request_consent(
            actor=actor,
            authorization_id=authorization.id,
            now=_NOW + timedelta(seconds=1),
        )
        authorized, _receipt = spend.authorize(
            actor=actor,
            authorization_id=authorization.id,
            receipt_id=str(uuid.uuid4()),
            now=_NOW + timedelta(seconds=2),
        )
        return actor, authorized.id


def _cleanup(business_id: str) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM business_offerings WHERE business_id=?",
            (business_id,),
        )
        conn.execute(
            "DELETE FROM business_capabilities WHERE business_id=?",
            (business_id,),
        )
        conn.execute(
            "DELETE FROM business_profiles WHERE business_id=?",
            (business_id,),
        )
        conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit(
            "POSTGRES_AD_SPEND_CONCURRENCY_FAILED: CLIENTPLATFORM_DB_ENGINE=postgres is required"
        )
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit(
            "POSTGRES_AD_SPEND_CONCURRENCY_FAILED: DATABASE_URL is required"
        )

    init_db()
    actor, authorization_id = _seed_authorized_spend()
    barrier = Barrier(2)

    def _enqueue() -> str:
        barrier.wait(timeout=15)
        with db() as conn:
            operation = AdSpendOperationRepository(conn).enqueue_launch(
                actor=actor,
                authorization_id=authorization_id,
                now=_NOW + timedelta(seconds=3),
            )
            return operation.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            operation_ids = list(executor.map(lambda _index: _enqueue(), range(2)))

        assert len(set(operation_ids)) == 1, operation_ids
        with db() as conn:
            operation_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM ad_spend_operations
                    WHERE business_id=? AND authorization_id=?
                      AND operation_type='launch'
                    """,
                    (actor.business_id, authorization_id),
                ).fetchone()[0]
            )
            status_row = conn.execute(
                """
                SELECT status,row_version
                FROM ad_spend_authorizations
                WHERE business_id=? AND id=?
                """,
                (actor.business_id, authorization_id),
            ).fetchone()
            audit_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM ad_audit_events
                    WHERE business_id=? AND subject_id=?
                      AND action='ad_spend_launch_queued'
                    """,
                    (actor.business_id, authorization_id),
                ).fetchone()[0]
            )

        assert operation_count == 1, operation_count
        assert str(status_row["status"]) == "launching", status_row
        assert int(status_row["row_version"] or 0) == 3, status_row
        assert audit_count == 1, audit_count
        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "postgres_ad_spend_launch_concurrency",
                    "parallel_requests": 2,
                    "operation_id": operation_ids[0],
                    "operation_count": operation_count,
                    "audit_count": audit_count,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_AD_SPEND_CONCURRENCY_OK")
        return 0
    finally:
        _cleanup(actor.business_id)


if __name__ == "__main__":
    raise SystemExit(main())
