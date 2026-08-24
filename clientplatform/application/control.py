from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clientplatform.domain.connections import (
    Connection,
    ConnectionPlatform,
    ConnectionStatus,
    ConnectionType,
    Dispatch,
)
from clientplatform.domain.customers import CustomerIdentityStatus, CustomerPlatform
from clientplatform.domain.programs import ContentKind, ProgramRecord
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import ConnectionRepository, DispatchOutboxRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.runtime.control_bot import CONTROL_BOT_CREDENTIAL_REFERENCE
from services.db import get_db


@dataclass(frozen=True, slots=True)
class PreparedProgramDelivery:
    program: ProgramRecord
    connection: Connection
    dispatch: Dispatch


def create_single_lesson_program(
    *,
    actor: TenantContext,
    program_title: str,
    lesson_title: str,
    content_kind: ContentKind | str,
    content_ref: str,
) -> ProgramRecord:
    """Create and publish one usable program atomically."""
    with get_db() as conn:
        programs = ProgramRepository(conn)
        program = programs.create_program(actor=actor, title=program_title)
        programs.add_lesson(
            actor=actor,
            program_id=program.id,
            title=lesson_title,
            content_kind=content_kind,
            content_ref=content_ref,
        )
        programs.publish_program(actor=actor, program_id=program.id)
        return programs.get_program(actor=actor, program_id=program.id)


def _shared_telegram_connection(
    *,
    actor: TenantContext,
    bot_id: int,
    repository: ConnectionRepository,
) -> Connection:
    selected: Connection | None = None
    for connection in repository.list_connections(actor=actor):
        if (
            connection.platform == ConnectionPlatform.TELEGRAM
            and connection.connection_type == ConnectionType.TELEGRAM_SHARED_BOT
            and connection.external_account_id == str(int(bot_id))
        ):
            selected = connection
            break
    if selected is None:
        selected = repository.create_connection(
            actor=actor,
            platform=ConnectionPlatform.TELEGRAM,
            connection_type=ConnectionType.TELEGRAM_SHARED_BOT,
            external_account_id=str(int(bot_id)),
            credential_reference=CONTROL_BOT_CREDENTIAL_REFERENCE,
            permissions=("send_message", "send_media"),
        )
    if selected.status != ConnectionStatus.ACTIVE:
        selected = repository.activate_connection(actor=actor, connection_id=selected.id)
    return selected


def _managed_telegram_connection(
    *,
    actor: TenantContext,
    repository: ConnectionRepository,
    conn: Any,
) -> Connection | None:
    rows = conn.execute(
        """
        SELECT c.id
        FROM managed_bots mb
        JOIN connections c
          ON c.id=mb.connection_id AND c.business_id=mb.business_id
         AND c.platform=mb.platform AND c.status='active'
        WHERE mb.business_id=? AND mb.platform='telegram' AND mb.status='active'
        ORDER BY mb.created_at, mb.id
        LIMIT 2
        """,
        (actor.business_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("business must have exactly one active managed Telegram bot")
    connection_id = str(rows[0]["id"] if hasattr(rows[0], "keys") else rows[0][0])
    for connection in repository.list_connections(actor=actor):
        if connection.id == connection_id:
            return connection
    raise ValueError("active managed Telegram bot connection was not found")


def _preferred_telegram_connection(
    *,
    actor: TenantContext,
    bot_id: int,
    repository: ConnectionRepository,
    conn: Any,
) -> Connection:
    managed = _managed_telegram_connection(
        actor=actor,
        repository=repository,
        conn=conn,
    )
    if managed is not None:
        return managed
    return _shared_telegram_connection(
        actor=actor,
        bot_id=bot_id,
        repository=repository,
    )


def _single_active_native_connection(
    *,
    actor: TenantContext,
    platform: ConnectionPlatform,
    repository: ConnectionRepository,
) -> Connection | None:
    active = [
        connection
        for connection in repository.list_connections(actor=actor)
        if connection.platform == platform and connection.status == ConnectionStatus.ACTIVE
    ]
    if len(active) > 1:
        raise ValueError(
            f"multiple active {platform.value} connections require explicit channel selection"
        )
    return active[0] if active else None


def _preferred_customer_delivery_route(
    *,
    actor: TenantContext,
    customer: Any,
    bot_id: int,
    repository: ConnectionRepository,
    conn: Any,
) -> tuple[Connection, Any]:
    identities_by_id = {identity.id: identity for identity in customer.identities}
    rows = conn.execute(
        """
        SELECT id, platform
        FROM customer_identities
        WHERE business_id=? AND customer_id=? AND status='active'
          AND platform IN ('telegram','vk','max')
        ORDER BY COALESCE(last_contact_at, updated_at, created_at) DESC, id
        """.strip(),
        (actor.business_id, customer.customer.id),
    ).fetchall()

    for row in rows:
        identity_id = str(row["id"] if hasattr(row, "keys") else row[0])
        platform_value = str(row["platform"] if hasattr(row, "keys") else row[1])
        identity = identities_by_id.get(identity_id)
        if identity is None or identity.status != CustomerIdentityStatus.ACTIVE:
            continue
        if platform_value == CustomerPlatform.TELEGRAM.value:
            return (
                _preferred_telegram_connection(
                    actor=actor,
                    bot_id=bot_id,
                    repository=repository,
                    conn=conn,
                ),
                identity,
            )
        if platform_value == CustomerPlatform.VK.value:
            connection = _single_active_native_connection(
                actor=actor,
                platform=ConnectionPlatform.VK,
                repository=repository,
            )
            if connection is not None:
                return connection, identity
            continue
        if platform_value == CustomerPlatform.MAX.value:
            connection = _single_active_native_connection(
                actor=actor,
                platform=ConnectionPlatform.MAX,
                repository=repository,
            )
            if connection is not None:
                return connection, identity

    raise ValueError(
        "customer has no active Telegram, VK or MAX identity with an active business connection"
    )


def prepare_program_delivery(
    *,
    actor: TenantContext,
    program_id: str,
    customer_id: str,
    bot_id: int,
) -> PreparedProgramDelivery:
    """Enroll one customer and dispatch through the most recently active usable channel."""
    with get_db() as conn:
        programs = ProgramRepository(conn)
        customers = CustomerRepository(conn)
        deliveries = DeliveryRepository(conn)
        connections = ConnectionRepository(conn)
        outbox = DispatchOutboxRepository(conn)

        program = programs.get_program(actor=actor, program_id=program_id)
        customer = customers.get_customer(actor=actor, customer_id=customer_id)
        connection, identity = _preferred_customer_delivery_route(
            actor=actor,
            customer=customer,
            bot_id=bot_id,
            repository=connections,
            conn=conn,
        )
        enrollment = deliveries.enroll_customer(
            actor=actor,
            program_id=program.program.id,
            customer_id=customer.customer.id,
        )
        pending = [
            delivery
            for delivery in enrollment.deliveries
            if delivery.status.value in {"pending", "failed"}
        ]
        if not pending:
            raise ValueError("program enrollment has no dispatchable lesson")
        dispatch = outbox.materialize(
            actor=actor,
            logical_delivery_id=pending[0].id,
            connection_id=connection.id,
            customer_identity_id=identity.id,
        )
        return PreparedProgramDelivery(
            program=program,
            connection=connection,
            dispatch=dispatch,
        )


@dataclass(frozen=True, slots=True)
class BusinessDeliverySummary:
    customers: int
    programs: int
    dispatch_pending: int
    dispatch_sent: int
    dispatch_attention: int


def business_delivery_summary(*, actor: TenantContext) -> BusinessDeliverySummary:
    with get_db() as conn:
        # Live membership resolution is mandatory before aggregate tenant queries.
        from clientplatform.infrastructure import TenancyRepository

        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()

        def count(sql: str, params: tuple[object, ...]) -> int:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return 0
            if hasattr(row, "keys"):
                return int(row["c"])
            return int(row[0])

        business_id = current.business_id
        return BusinessDeliverySummary(
            customers=count(
                "SELECT COUNT(*) AS c FROM customers WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            programs=count(
                "SELECT COUNT(*) AS c FROM programs WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            dispatch_pending=count(
                """
                SELECT COUNT(*) AS c FROM delivery_dispatch_outbox
                WHERE business_id=? AND status IN ('pending', 'retry', 'sending')
                """,
                (business_id,),
            ),
            dispatch_sent=count(
                "SELECT COUNT(*) AS c FROM delivery_dispatch_outbox WHERE business_id=? AND status='sent'",
                (business_id,),
            ),
            dispatch_attention=count(
                "SELECT COUNT(*) AS c FROM delivery_dispatch_outbox WHERE business_id=? AND status='dead'",
                (business_id,),
            ),
        )
