from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from clientplatform.domain.connections import (
    Connection,
    ConnectionPlatform,
    ConnectionStatus,
    ConnectionType,
)
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import ConnectionRepository
from clientplatform.infrastructure.connection_credentials import (
    ConnectionCredentialStore,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    ManagedBotCredentialVault,
)
from clientplatform.infrastructure.messenger_channel_repository import (
    MessengerChannelRepository,
)
from runtime.messenger_max_sender import MaxBotSender
from runtime.messenger_vk_sender import VkBotSender
from services.db import get_db


@dataclass(frozen=True, slots=True)
class NativeMessengerConnection:
    connection: Connection
    route: MessengerIngressRoute
    webhook_url: str
    display_name: str | None = None
    username: str | None = None


def _public_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("messenger public base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("messenger public base URL is invalid")
    return normalized


def _webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def _webhook_url(base_url: str, platform: ConnectionPlatform, route_id: str) -> str:
    return f"{base_url}/clientplatform/webhooks/{platform.value}/{route_id}"

def _persist_native_connection(
    *,
    actor: TenantContext,
    platform: ConnectionPlatform,
    connection_type: ConnectionType,
    external_account_id: str,
    provider_token: str,
    webhook_secret: str,
    confirmation_code: str | None,
    credential_vault: ManagedBotCredentialVault | None,
) -> tuple[Connection, MessengerIngressRoute]:
    with get_db() as conn:
        credentials = ConnectionCredentialStore(conn, vault=credential_vault)
        token_reference = credentials.put(
            actor=actor,
            platform=platform,
            external_account_id=external_account_id,
            purpose="provider_token",
            plaintext=provider_token,
        )
        webhook_reference = credentials.put(
            actor=actor,
            platform=platform,
            external_account_id=external_account_id,
            purpose="webhook_secret",
            plaintext=webhook_secret,
        )
        confirmation_reference = None
        if confirmation_code is not None:
            confirmation_reference = credentials.put(
                actor=actor,
                platform=platform,
                external_account_id=external_account_id,
                purpose="confirmation_code",
                plaintext=confirmation_code,
            )

        connections = ConnectionRepository(conn)
        connection = connections.create_connection(
            actor=actor,
            platform=platform,
            connection_type=connection_type,
            external_account_id=external_account_id,
            credential_reference=token_reference,
            permissions=("receive_message", "send_media", "send_message"),
        )
        if connection.credential_reference != token_reference:
            connection = connections.replace_credential_reference(
                actor=actor,
                connection_id=connection.id,
                credential_reference=token_reference,
            )
        if connection.status != ConnectionStatus.ACTIVE:
            connection = connections.activate_connection(
                actor=actor,
                connection_id=connection.id,
            )

        routes = MessengerChannelRepository(conn)
        route = routes.register_route(
            actor=actor,
            connection_id=connection.id,
            external_route_id=external_account_id,
            webhook_secret_reference=webhook_reference,
            confirmation_code_reference=confirmation_reference,
        )
        if route.status != "active":
            route = routes.activate_route(actor=actor, route_id=route.id)
        return connection, route


def _disable_after_provider_failure(
    *,
    actor: TenantContext,
    connection_id: str,
    route_id: str,
) -> None:
    with get_db() as conn:
        routes = MessengerChannelRepository(conn)
        connections = ConnectionRepository(conn)
        try:
            routes.disable_route(actor=actor, route_id=route_id)
        finally:
            connections.disable_connection(
                actor=actor,
                connection_id=connection_id,
            )

async def provision_max_channel(
    *,
    actor: TenantContext,
    provider_token: str,
    public_base_url: str,
    sender: MaxBotSender | None = None,
    credential_vault: ManagedBotCredentialVault | None = None,
) -> NativeMessengerConnection:
    base_url = _public_base_url(public_base_url)
    token = str(provider_token or "").strip()
    if not token:
        raise ValueError("MAX provider token must not be empty")
    provider = sender or MaxBotSender(token=token)
    identity = await provider.get_me()
    external_account_id = str(identity.get("user_id") or "").strip()
    webhook_secret = _webhook_secret()
    connection, route = _persist_native_connection(
        actor=actor,
        platform=ConnectionPlatform.MAX,
        connection_type=ConnectionType.MAX_PERSONAL_BOT,
        external_account_id=external_account_id,
        provider_token=token,
        webhook_secret=webhook_secret,
        confirmation_code=None,
        credential_vault=credential_vault,
    )
    webhook_url = _webhook_url(base_url, ConnectionPlatform.MAX, route.id)
    try:
        await provider.ensure_webhook_subscription(
            url=webhook_url,
            secret=webhook_secret,
        )
    except Exception:
        _disable_after_provider_failure(
            actor=actor,
            connection_id=connection.id,
            route_id=route.id,
        )
        raise
    display_name = str(identity.get("first_name") or identity.get("name") or "").strip() or None
    username = str(identity.get("username") or "").strip() or None
    return NativeMessengerConnection(
        connection=connection,
        route=route,
        webhook_url=webhook_url,
        display_name=display_name,
        username=username,
    )


async def provision_vk_channel(
    *,
    actor: TenantContext,
    group_id: str | int,
    provider_token: str,
    public_base_url: str,
    sender: VkBotSender | None = None,
    credential_vault: ManagedBotCredentialVault | None = None,
) -> NativeMessengerConnection:
    base_url = _public_base_url(public_base_url)
    token = str(provider_token or "").strip()
    if not token:
        raise ValueError("VK provider token must not be empty")
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id.isdigit() or int(normalized_group_id) <= 0:
        raise ValueError("VK group id must be a positive integer")
    provider = sender or VkBotSender(token=token)
    group = await provider.verify_community(normalized_group_id)
    confirmation_code = await provider.get_callback_confirmation_code(
        normalized_group_id
    )
    webhook_secret = _webhook_secret()
    connection, route = _persist_native_connection(
        actor=actor,
        platform=ConnectionPlatform.VK,
        connection_type=ConnectionType.VK_COMMUNITY,
        external_account_id=normalized_group_id,
        provider_token=token,
        webhook_secret=webhook_secret,
        confirmation_code=confirmation_code,
        credential_vault=credential_vault,
    )
    webhook_url = _webhook_url(base_url, ConnectionPlatform.VK, route.id)
    try:
        await provider.ensure_callback_server(
            group_id=normalized_group_id,
            url=webhook_url,
            secret=webhook_secret,
        )
    except Exception:
        _disable_after_provider_failure(
            actor=actor,
            connection_id=connection.id,
            route_id=route.id,
        )
        raise
    display_name = str(group.get("name") or "").strip() or None
    return NativeMessengerConnection(
        connection=connection,
        route=route,
        webhook_url=webhook_url,
        display_name=display_name,
    )


__all__ = [
    "NativeMessengerConnection",
    "provision_max_channel",
    "provision_vk_channel",
]
