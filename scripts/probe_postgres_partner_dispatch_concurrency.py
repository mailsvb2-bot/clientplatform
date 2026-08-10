from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from queue import Queue
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerChannel,
    PartnerContentPack,
)
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from services.db import get_db, get_db_ro
from services.schema import init_db


class ProbeFailure(RuntimeError):
    pass


def _postgres_configured() -> bool:
    url = (os.getenv("DATABASE_URL") or "").strip().lower()
    return url.startswith("postgresql://") or url.startswith("postgres://")


def _setup_fixture() -> tuple[object, str, str]:
    owner_user_id = 9_120_001
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id,username,joined_at)
            VALUES(?,?,'2026-08-10T00:00:00+00:00')
            ON CONFLICT(user_id) DO NOTHING
            """,
            (owner_user_id, "partner-pg-probe"),
        )
        tenancy = TenancyRepository(conn)
        access = tenancy.create_business(
            owner_user_id=owner_user_id,
            name=f"Partner PG Probe {uuid4().hex[:8]}",
        )
        actor = tenancy.resolve_context(
            user_id=owner_user_id,
            business_id=access.business.id,
        )
        campaign = PartnerRepository(conn).create_campaign(
            actor=actor,
            name="Concurrent partner dispatch",
            goal=PartnerCampaignGoal(
                target_count=1,
                audience_terms=("psychology",),
            ),
        )
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=actor.business_id,
            campaign_id=campaign.id,
            name="Concurrency Partner",
            source_url="https://example.test/partner-concurrency",
            audience_summary="psychology audience",
            channel=PartnerChannel.TELEGRAM,
            contact_value="7912001",
            contact_basis=ContactBasis.OPTED_IN,
            follower_count=1000,
        )
        score = score_partner(provisional, campaign.goal)
        candidate = PartnerRepository(conn).upsert_candidate(
            actor=actor,
            campaign=campaign,
            name=provisional.name,
            source_url=provisional.source_url,
            audience_summary=provisional.audience_summary,
            recent_topic="",
            channel=provisional.channel,
            contact_value=provisional.contact_value,
            contact_basis=provisional.contact_basis,
            follower_count=provisional.follower_count,
            tags=("probe",),
            competitor=False,
            score=score,
        )
        PartnerRepository(conn).save_content_pack(
            actor=actor,
            campaign_id=campaign.id,
            pack=PartnerContentPack(
                candidate_id=candidate.id,
                subject="Probe",
                outreach_message="Проверка конкурентной идемпотентности партнёрской отправки.",
                ready_post="Probe post",
                followup_message="Probe follow-up",
                collaboration_angle="Probe",
                cta="Probe",
            ),
        )
        connection = ConnectionRepository(conn).create_connection(
            actor=actor,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id="partner-pg-probe-bot",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_PG_PROBE",
            permissions=("send_messages",),
        )
        connection = ConnectionRepository(conn).activate_connection(
            actor=actor,
            connection_id=connection.id,
        )
        return actor, candidate.id, connection.id


def _run_concurrently(function) -> list[object]:
    barrier = threading.Barrier(2)
    queue: Queue[tuple[bool, object]] = Queue()

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            queue.put((True, function()))
        except BaseException as exc:  # validator: allow-wide-except
            queue.put((False, exc))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        if thread.is_alive():
            raise ProbeFailure("partner dispatch concurrency worker hung")

    results: list[object] = []
    while not queue.empty():
        ok, value = queue.get_nowait()
        if not ok:
            raise ProbeFailure(f"partner dispatch worker failed: {type(value).__name__}")
        results.append(value)
    if len(results) != 2:
        raise ProbeFailure(f"expected two concurrency results, got {len(results)}")
    return results


def _backend_pid(conn) -> int:
    row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
    return int(row["pid"] if hasattr(row, "keys") else row[0])


def main() -> None:
    if not _postgres_configured():
        raise SystemExit("DATABASE_URL must point to PostgreSQL")
    init_db()
    actor, candidate_id, connection_id = _setup_fixture()

    def enqueue():
        with get_db() as conn:
            pid = _backend_pid(conn)
            dispatch = DispatchOutboxRepository(conn).materialize_partner_outreach(
                actor=actor,
                candidate_id=candidate_id,
                connection_id=connection_id,
            )
            return dispatch, pid

    enqueue_results = _run_concurrently(enqueue)
    enqueue_ids = {
        str(getattr(result[0], "id", ""))
        for result in enqueue_results
    }
    enqueue_pids = {int(result[1]) for result in enqueue_results}
    if len(enqueue_pids) != 2:
        raise ProbeFailure(
            f"enqueue did not use two PostgreSQL backends: pids={sorted(enqueue_pids)!r}"
        )
    if len(enqueue_ids) != 1 or "" in enqueue_ids:
        raise ProbeFailure(
            f"concurrent enqueue was not idempotent: ids={sorted(enqueue_ids)!r}"
        )
    with get_db_ro() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM provider_dispatch_outbox
            WHERE business_id=? AND partner_candidate_id=?
            """,
            (actor.business_id, candidate_id),
        ).fetchone()
        total = int(count["total"] if hasattr(count, "keys") else count[0])
    if total != 1:
        raise ProbeFailure(f"expected one persisted partner dispatch, got {total}")

    def claim():
        with get_db() as conn:
            pid = _backend_pid(conn)
            claimed = DispatchOutboxRepository(conn).claim_due(limit=1)
            ids = [
                item.dispatch.id
                for item in claimed
                if isinstance(item, ClaimedProviderDispatch)
                and item.dispatch.source_id == candidate_id
            ]
            return ids, pid

    claim_results = _run_concurrently(claim)
    claim_pids = {int(result[1]) for result in claim_results}
    if len(claim_pids) != 2:
        raise ProbeFailure(
            f"claim did not use two PostgreSQL backends: pids={sorted(claim_pids)!r}"
        )
    claimed_ids = [item for result in claim_results for item in result[0]]
    if len(claimed_ids) != 1 or claimed_ids[0] not in enqueue_ids:
        raise ProbeFailure(
            f"concurrent claim duplicated or lost work: claimed={claimed_ids!r}"
        )

    print(
        "POSTGRES_PARTNER_DISPATCH_CONCURRENCY_OK "
        f"dispatch_id={next(iter(enqueue_ids))} claims={len(claimed_ids)} "
        f"enqueue_backends={len(enqueue_pids)} claim_backends={len(claim_pids)}"
    )


if __name__ == "__main__":
    main()
