from __future__ import annotations

from clientplatform.domain.connections import (
    Connection,
    ConnectionPlatform,
    ConnectionType,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.connection_credentials import ConnectionCredentialStore
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport.email import (
    SmtpCredential,
    SmtpEmailClient,
    normalize_email_address,
)
from services.db import get_db, get_db_ro


def provision_email_smtp_connection(
    *,
    actor: TenantContext,
    sender_email: str,
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    security: str = "ssl",
    sender_name: str = "",
) -> Connection:
    """Store SMTP material encrypted and create a pending business connection.

    The connection deliberately remains ``pending`` until
    ``verify_and_activate_email_smtp_connection`` proves that the provider
    accepts the credentials. Raw SMTP material never enters the connections
    table and must never be logged by callers.
    """

    sender = normalize_email_address(sender_email)
    credential = SmtpCredential(
        host=smtp_host,
        port=smtp_port,
        username=username,
        password=password,
        sender_email=sender,
        sender_name=sender_name,
        security=security,
    )
    with get_db() as conn:
        credential_reference = ConnectionCredentialStore(conn).put(
            actor=actor,
            platform=ConnectionPlatform.EMAIL,
            external_account_id=sender,
            purpose="smtp_credentials",
            plaintext=credential.to_json(),
        )
        repository = ConnectionRepository(conn)
        connection = repository.create_connection(
            actor=actor,
            platform=ConnectionPlatform.EMAIL,
            connection_type=ConnectionType.EMAIL_SMTP,
            external_account_id=sender,
            credential_reference=credential_reference,
            permissions=("send_email",),
        )
        connection = repository.replace_credential_reference(
            actor=actor,
            connection_id=connection.id,
            credential_reference=credential_reference,
        )
        return repository.mark_connection_pending_for_reverification(
            actor=actor,
            connection_id=connection.id,
        )


async def verify_and_activate_email_smtp_connection(
    *,
    actor: TenantContext,
    connection_id: str,
    client: SmtpEmailClient | None = None,
) -> Connection:
    """Probe SMTP auth first, then activate exactly that tenant connection."""

    with get_db_ro() as conn:
        matches = [
            item
            for item in ConnectionRepository(conn).list_connections(actor=actor)
            if item.id == str(connection_id)
        ]
    if len(matches) != 1:
        raise ValueError("email connection was not found in the active business")
    connection = matches[0]
    if (
        connection.platform != ConnectionPlatform.EMAIL
        or connection.connection_type != ConnectionType.EMAIL_SMTP
    ):
        raise ValueError("connection is not an SMTP email connection")
    resolved = EnvironmentCredentialProvider().resolve(connection.credential_reference)
    credential = SmtpCredential.from_json(resolved)
    if credential.sender_email != normalize_email_address(connection.external_account_id):
        raise ValueError("SMTP credential sender does not match the connection account")
    await (client or SmtpEmailClient()).probe(credential=credential)
    with get_db() as conn:
        return ConnectionRepository(conn).activate_connection(
            actor=actor,
            connection_id=connection.id,
        )


__all__ = [
    "provision_email_smtp_connection",
    "verify_and_activate_email_smtp_connection",
]
