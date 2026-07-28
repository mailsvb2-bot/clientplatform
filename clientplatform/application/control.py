from __future__ import annotations

from dataclasses import dataclass

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


def prepare_program_delivery(
    *,
    actor: TenantContext,
    program_id: str,
    customer_id: str,
    bot_id: int,
) -> PreparedProgramDelivery:
    """Enroll one customer and materialize the first Telegram dispatch atomically."""
    with get_db() as conn:
        programs = ProgramRepository(conn)
        customers = CustomerRepository(conn)
        deliveries = DeliveryRepository(conn)
        connections = ConnectionRepository(conn)
        outbox = DispatchOutboxRepository(conn)

        program = programs.get_program(actor=actor, program_id=program_id)
        customer = customers.get_customer(actor=actor, customer_id=customer_id)
        telegram_identities = [
            identity
            for identity in customer.identities
            if identity.platform == CustomerPlatform.TELEGRAM
            and identity.status == CustomerIdentityStatus.ACTIVE
        ]
        if len(telegram_identities) != 1:
            raise ValueError("customer must have exactly one active Telegram identity")
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
        connection = _shared_telegram_connection(
            actor=actor,
            bot_id=bot_id,
            repository=connections,
        )
        dispatch = outbox.materialize(
            actor=actor,
            logical_delivery_id=pending[0].id,
            connection_id=connection.id,
            customer_identity_id=telegram_identities[0].id,
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
