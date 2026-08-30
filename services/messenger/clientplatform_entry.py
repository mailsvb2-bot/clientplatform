from __future__ import annotations

from dataclasses import dataclass

from clientplatform.application.activity import save_business_profile
from clientplatform.application.native_member_interactions import render_native_member_interaction
from clientplatform.application.tenancy import (
    create_business,
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.tenancy import TenancyError
from clientplatform.runtime.native_messenger_setup_links import (
    NativeMessengerSetupLinkService,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from config.settings import settings
from services.messenger.entrypoints import register_user_entry
from services.messenger.platforms import normalize_platform
from services.messenger.text_ui import MessengerReply

_START_EVENT_TYPES = frozenset(
    {
        "bot_started",
        "bot_start",
        "chat_started",
        "conversation_started",
    }
)
_START_ALIASES = frozenset(
    {
        "start",
        "/start",
        "menu",
        "/menu",
        "старт",
        "начать",
        "главное меню",
    }
)
_BUSINESS_PREFIXES = (
    "business ",
    "/business ",
    "бизнес ",
    "создать бизнес ",
)
_ACTIVITY_PREFIXES = (
    "activity ",
    "/activity ",
    "деятельность ",
)
_OWNER_CONTROL_ALIASES = frozenset(
    {
        "menu",
        "/menu",
        "меню",
        "кабинет",
        "админ",
        "/admin",
        "работа",
        "рост",
        "управление",
        "команда",
        "сегодня",
        "клиенты",
        "записи",
        "программы",
        "мессенджеры",
        "обращения",
        "продажи",
        "обращения и продажи",
    }
)
_PLATFORM_TITLES = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
}


@dataclass(frozen=True, slots=True)
class ClientPlatformEntryCommand:
    action: str
    value: str = ""


def parse_clientplatform_entry_command(
    text: str | None,
    *,
    event_type: str | None = None,
) -> ClientPlatformEntryCommand | None:
    """Recognize only the channel-neutral ClientPlatform entry surface."""

    raw = " ".join(str(text or "").strip().split())
    lowered = raw.casefold().replace("ё", "е")
    normalized_event = str(event_type or "").strip().lower()

    if normalized_event in _START_EVENT_TYPES and not raw:
        return ClientPlatformEntryCommand("start")
    if lowered in _START_ALIASES:
        return ClientPlatformEntryCommand("start")
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
        return ClientPlatformEntryCommand("start", payload)
    for prefix in _BUSINESS_PREFIXES:
        if lowered.startswith(prefix):
            return ClientPlatformEntryCommand(
                "create_business",
                raw[len(prefix) :].strip(),
            )
    if lowered in {"business", "/business", "бизнес", "создать бизнес"}:
        return ClientPlatformEntryCommand("create_business")
    for prefix in _ACTIVITY_PREFIXES:
        if lowered.startswith(prefix):
            return ClientPlatformEntryCommand(
                "describe_business",
                raw[len(prefix) :].strip(),
            )
    if lowered in {"activity", "/activity", "деятельность"}:
        return ClientPlatformEntryCommand("describe_business")
    if lowered.startswith("cpm:") or lowered in _OWNER_CONTROL_ALIASES:
        return ClientPlatformEntryCommand("owner_control", raw)
    return None


def _platform_title(platform: str) -> str:
    normalized = normalize_platform(platform)
    return _PLATFORM_TITLES.get(normalized, normalized.upper())


def _entry_text(*, platform: str, accesses: list[object]) -> str:
    title = _platform_title(platform)
    if not accesses:
        return (
            "Добро пожаловать в ClientPlatform.\n\n"
            f"Вы вошли через {title}. Здесь можно начать работу без перехода "
            "в Telegram.\n\n"
            "Чтобы создать своё рабочее пространство, отправьте одним сообщением:\n"
            "бизнес <название>\n\n"
            "Например: бизнес Психологическая практика Анны"
        )

    names = [str(access.business.name) for access in accesses]
    business_lines = "\n".join(f"• {name}" for name in names)
    return (
        "ClientPlatform\n\n"
        f"Вход через {title} подтверждён.\n\n"
        "Ваши рабочие пространства:\n"
        f"{business_lines}\n\n"
        "Команда start или /start в любой момент снова открывает этот вход."
    )


def _connection_platform(platform: str) -> ConnectionPlatform:
    normalized = normalize_platform(platform)
    return ConnectionPlatform(normalized)


def _interaction_reply(
    interaction: CustomerInteractionMessage,
    *,
    business_id: str,
) -> MessengerReply:
    return MessengerReply(
        kind="clientplatform_interaction",
        text=interaction.text,
        meta={
            "interaction": interaction.to_json(),
            "business_id": str(business_id),
        },
    )


def _single_business_actor(*, user_id: int, accesses: list[object]):
    if len(accesses) != 1:
        return None
    return resolve_tenant_context(
        user_id=int(user_id),
        business_id=str(accesses[0].business.id),
    )


def _owner_control_reply(
    *,
    canonical_user_id: int,
    platform: str,
    accesses: list[object],
    raw_text: str,
) -> MessengerReply | None:
    actor = _single_business_actor(user_id=canonical_user_id, accesses=accesses)
    if actor is None:
        return None
    setup_links = NativeMessengerSetupLinkService(
        credential_provider=EnvironmentCredentialProvider(),
    )

    def _issue_setup_command(
        current_actor,
        target_platform: ConnectionPlatform,
        setup_key: str,
    ) -> str:
        return setup_links.issue_command(
            actor=current_actor,
            platform=target_platform,
            idempotency_key=setup_key,
        )

    interaction = render_native_member_interaction(
        actor=actor,
        raw_text=raw_text or "cpm:menu",
        interaction_key=f"official:{normalize_platform(platform)}:{canonical_user_id}",
        current_platform=_connection_platform(platform),
        setup_issuer=_issue_setup_command,
    )
    return _interaction_reply(interaction, business_id=actor.business_id)


def _normalized_business_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _existing_business(accesses: list[object], name: str) -> object | None:
    expected = _normalized_business_name(name)
    for access in accesses:
        business = getattr(access, "business", None)
        if _normalized_business_name(getattr(business, "name", "")) == expected:
            return access
    return None


def handle_clientplatform_entry(
    user_id: int,
    *,
    platform: str,
    external_user_id: str | None,
    text: str | None,
    event_type: str | None = None,
    username: str | None = None,
    display_name: str | None = None,
    first_name: str | None = None,
) -> tuple[int, list[MessengerReply]]:
    """Register a channel identity and return the ClientPlatform entry response."""

    command = parse_clientplatform_entry_command(text, event_type=event_type)
    if command is None:
        raise ValueError("message is not a ClientPlatform entry command")

    entry = register_user_entry(
        int(user_id),
        platform=platform,
        external_user_id=external_user_id,
        username=username,
        display_name=display_name,
        first_name=first_name,
        start_payload=command.value if command.action == "start" else None,
    )
    canonical_user_id = int(entry.user_id)

    accesses = list(list_accessible_businesses(user_id=canonical_user_id))
    if command.action == "owner_control":
        reply = _owner_control_reply(
            canonical_user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
            raw_text=command.value,
        )
        if reply is not None:
            return canonical_user_id, [reply]
        if not accesses:
            return canonical_user_id, [MessengerReply(text=_entry_text(platform=platform, accesses=[]))]
        return canonical_user_id, [MessengerReply(text=_entry_text(platform=platform, accesses=accesses))]

    if command.action == "describe_business":
        actor = _single_business_actor(user_id=canonical_user_id, accesses=accesses)
        if actor is None:
            if not accesses:
                return canonical_user_id, [
                    MessengerReply(text="Сначала создайте бизнес: бизнес <название>")
                ]
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        "У Вас несколько бизнесов. Выбор рабочего пространства для "
                        "официального VK/MAX-входа будет добавлен следующим этапом. "
                        "Пока откройте нужный бизнес через основной ClientPlatform."
                    )
                )
            ]
        if not command.value:
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        "Опишите, чем Вы занимаетесь, после слова «деятельность».\n\n"
                        "Например: деятельность Ремонтирую автомобили и принимаю "
                        "заказы на обслуживание"
                    )
                )
            ]
        save_business_profile(
            actor=actor,
            activity_description=command.value,
            timezone_name=settings.TIMEZONE,
        )
        reply = _owner_control_reply(
            canonical_user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
            raw_text="cpm:menu",
        )
        if reply is None:
            raise RuntimeError("single-business owner control could not be rendered")
        return canonical_user_id, [
            MessengerReply(text=f"Описание сохранено: {command.value}"),
            reply,
        ]

    if command.action == "create_business":
        if not command.value:
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        "Напишите название после слова «бизнес».\n\n"
                        "Например: бизнес Автосервис Север"
                    )
                )
            ]
        existing = _existing_business(accesses, command.value)
        if existing is not None:
            existing_name = str(existing.business.name)
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        f"Рабочее пространство «{existing_name}» уже существует.\n\n"
                        "Отправьте start, чтобы открыть вход ClientPlatform."
                    )
                )
            ]
        try:
            access = create_business(
                owner_user_id=canonical_user_id,
                name=command.value,
            )
        except (TenancyError, ValueError):
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        "Не удалось создать рабочее пространство с таким названием. "
                        "Проверьте название и попробуйте ещё раз."
                    )
                )
            ]
        return canonical_user_id, [
            MessengerReply(
                text=(
                    f"Готово. Рабочее пространство «{access.business.name}» создано.\n\n"
                    "Теперь опишите, чем Вы занимаетесь, одним сообщением:\n"
                    "деятельность <описание>\n\n"
                    "Например: деятельность Консультирую родителей по вопросам сна детей"
                )
            )
        ]

    if command.action == "start" and len(accesses) == 1:
        reply = _owner_control_reply(
            canonical_user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
            raw_text="cpm:menu",
        )
        if reply is not None:
            return canonical_user_id, [reply]

    return canonical_user_id, [
        MessengerReply(text=_entry_text(platform=platform, accesses=accesses))
    ]


__all__ = [
    "ClientPlatformEntryCommand",
    "handle_clientplatform_entry",
    "parse_clientplatform_entry_command",
]
