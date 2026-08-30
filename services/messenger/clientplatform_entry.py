from __future__ import annotations

import hashlib
from dataclasses import dataclass

from clientplatform.application.activity import get_business_profile, save_business_profile
from clientplatform.application.control_callbacks import token_uuid, uuid_token
from clientplatform.application.native_member_interactions import (
    recognizes_native_member_interaction,
    render_native_member_interaction,
)
from clientplatform.application.tenancy import (
    create_business,
    get_owner_control_workspace,
    list_accessible_businesses,
    resolve_tenant_context,
    set_owner_control_workspace,
)
from clientplatform.domain.activity import ActivityNotFound
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.tenancy import TenantPermissionDenied, TenancyError
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
        if payload.casefold().startswith("bridge_"):
            return ClientPlatformEntryCommand("start", payload)
        # Non-owner deep links (for example cpa_* customer acquisition) must
        # continue to their dedicated route instead of being swallowed by the
        # official ClientPlatform owner bootstrap.
        return None
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
    if lowered.startswith("cpw:"):
        return ClientPlatformEntryCommand("workspace", raw)
    if lowered.startswith("cpm:") or lowered in _OWNER_CONTROL_ALIASES:
        return ClientPlatformEntryCommand("owner_control", raw)
    if recognizes_native_member_interaction(raw):
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
    business_id: str | None = None,
) -> MessengerReply:
    meta = {"interaction": interaction.to_json()}
    if business_id:
        meta["business_id"] = str(business_id)
    return MessengerReply(
        kind="clientplatform_interaction",
        text=interaction.text,
        meta=meta,
    )


def _business_access_by_id(accesses: list[object], business_id: str):
    expected = str(business_id or "").strip()
    return next(
        (access for access in accesses if str(access.business.id) == expected),
        None,
    )


def _business_access_by_token(accesses: list[object], token: str):
    try:
        business_id = token_uuid(token)
    except (TypeError, ValueError):
        return None
    return _business_access_by_id(accesses, business_id)


def _active_business_id(
    *,
    user_id: int,
    platform: str,
    accesses: list[object],
) -> str | None:
    if len(accesses) == 1:
        return str(accesses[0].business.id)
    if not accesses:
        return None
    selected = get_owner_control_workspace(
        user_id=int(user_id),
        platform=normalize_platform(platform),
    )
    if selected is None:
        return None
    access = _business_access_by_id(accesses, selected)
    return None if access is None else str(access.business.id)


def _remember_business(
    *,
    user_id: int,
    platform: str,
    business_id: str,
) -> str:
    return set_owner_control_workspace(
        user_id=int(user_id),
        platform=normalize_platform(platform),
        business_id=str(business_id),
    )


def _business_actor(
    *,
    user_id: int,
    accesses: list[object],
    business_id: str | None = None,
):
    if business_id is None:
        if len(accesses) != 1:
            return None
        access = accesses[0]
    else:
        access = _business_access_by_id(accesses, business_id)
        if access is None:
            return None
    return resolve_tenant_context(
        user_id=int(user_id),
        business_id=str(access.business.id),
    )


def _business_selector_reply(accesses: list[object], *, page: int = 0) -> MessengerReply:
    page_size = 8
    page_count = max(1, (len(accesses) + page_size - 1) // page_size)
    safe_page = min(max(int(page), 0), page_count - 1)
    current = accesses[safe_page * page_size : (safe_page + 1) * page_size]
    rows = [
        (
            CustomerInteractionButton(
                label=str(access.business.name)[:40],
                command=f"cpw:open:{uuid_token(str(access.business.id))}",
            ),
        )
        for access in current
    ]
    navigation = []
    if safe_page > 0:
        navigation.append(
            CustomerInteractionButton(
                label="⬅️ Назад",
                command=f"cpw:list:{safe_page - 1}",
            )
        )
    if safe_page + 1 < page_count:
        navigation.append(
            CustomerInteractionButton(
                label="Вперёд ➡️",
                command=f"cpw:list:{safe_page + 1}",
            )
        )
    if navigation:
        rows.append(tuple(navigation))
    interaction = CustomerInteractionMessage(
        text=(
            "Выберите бизнес, с которым хотите работать.\n\n"
            "ClientPlatform проверит Ваш доступ заново при каждом выборе."
            + (f"\n\nСтраница {safe_page + 1}/{page_count}" if page_count > 1 else "")
        ),
        rows=tuple(rows),
    )
    return _interaction_reply(interaction)


def _official_interaction_key(
    *,
    platform: str,
    canonical_user_id: int,
    event_key: str | None,
    raw_text: str,
) -> str:
    normalized_platform = normalize_platform(platform)
    material = "\x1f".join(
        (
            normalized_platform,
            str(int(canonical_user_id)),
            str(event_key or "direct").strip(),
            str(raw_text or "").strip(),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"official:{normalized_platform}:{int(canonical_user_id)}:{digest}"


def _owner_control_reply(
    *,
    canonical_user_id: int,
    platform: str,
    accesses: list[object],
    raw_text: str,
    business_id: str | None = None,
    interaction_key: str,
) -> MessengerReply | None:
    actor = _business_actor(
        user_id=canonical_user_id,
        accesses=accesses,
        business_id=business_id,
    )
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
        interaction_key=interaction_key,
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
    event_key: str | None = None,
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
    interaction_key = _official_interaction_key(
        platform=platform,
        canonical_user_id=canonical_user_id,
        event_key=event_key,
        raw_text=command.value or str(text or command.action),
    )
    if command.action == "workspace":
        parts = command.value.split(":", 3)
        if len(parts) >= 3 and parts[:2] == ["cpw", "list"]:
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
            return canonical_user_id, [_business_selector_reply(accesses, page=page)]
        if len(parts) >= 3 and parts[:2] == ["cpw", "open"]:
            access = _business_access_by_token(accesses, parts[2])
            if access is None:
                return canonical_user_id, [
                    MessengerReply(text="Этот бизнес больше недоступен для Вашего аккаунта."),
                    _business_selector_reply(accesses),
                ]
            _remember_business(
                user_id=canonical_user_id,
                platform=platform,
                business_id=str(access.business.id),
            )
            reply = _owner_control_reply(
                canonical_user_id=canonical_user_id,
                platform=platform,
                accesses=accesses,
                raw_text="cpm:menu",
                business_id=str(access.business.id),
                interaction_key=interaction_key,
            )
            if reply is None:
                raise RuntimeError("selected business could not be resolved")
            return canonical_user_id, [reply]
        if len(parts) == 4 and parts[:2] == ["cpw", "act"]:
            access = _business_access_by_token(accesses, parts[2])
            if access is None:
                return canonical_user_id, [
                    MessengerReply(text="Доступ к выбранному бизнесу изменился. Выберите бизнес снова."),
                    _business_selector_reply(accesses),
                ]
            inner = parts[3]
            if not inner.startswith("cpm:"):
                return canonical_user_id, [_business_selector_reply(accesses)]
            _remember_business(
                user_id=canonical_user_id,
                platform=platform,
                business_id=str(access.business.id),
            )
            reply = _owner_control_reply(
                canonical_user_id=canonical_user_id,
                platform=platform,
                accesses=accesses,
                raw_text=inner,
                business_id=str(access.business.id),
                interaction_key=interaction_key,
            )
            if reply is None:
                raise RuntimeError("scoped business action could not be resolved")
            return canonical_user_id, [reply]
        return canonical_user_id, [_business_selector_reply(accesses)]

    if command.action == "owner_control":
        active_business_id = _active_business_id(
            user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
        )
        reply = _owner_control_reply(
            canonical_user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
            raw_text=command.value,
            business_id=active_business_id,
            interaction_key=interaction_key,
        )
        if reply is not None:
            return canonical_user_id, [reply]
        if not accesses:
            return canonical_user_id, [MessengerReply(text=_entry_text(platform=platform, accesses=[]))]
        return canonical_user_id, [_business_selector_reply(accesses)]

    if command.action == "describe_business":
        active_business_id = _active_business_id(
            user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
        )
        actor = _business_actor(
            user_id=canonical_user_id,
            accesses=accesses,
            business_id=active_business_id,
        )
        if actor is None:
            if not accesses:
                return canonical_user_id, [
                    MessengerReply(text="Сначала создайте бизнес: бизнес <название>")
                ]
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        "У Вас несколько бизнесов. Сначала выберите рабочее пространство "
                        "кнопкой ниже, затем повторите описание деятельности."
                    )
                ),
                _business_selector_reply(accesses),
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
        # Existing profiles use the canonical native edit path so authorization,
        # timezone preservation and user-facing errors stay channel-neutral. The
        # first profile is special: the native renderer intentionally converts
        # ActivityNotFound into a stale interaction, so detect that bootstrap
        # state before rendering instead of relying on the exception to escape.
        try:
            actor.assert_can_manage_business()
        except TenantPermissionDenied:
            activity_reply = _owner_control_reply(
                canonical_user_id=canonical_user_id,
                platform=platform,
                accesses=accesses,
                raw_text=f"деятельность {command.value}",
                business_id=actor.business_id,
                interaction_key=interaction_key,
            )
            if activity_reply is None:
                raise RuntimeError("single-business activity permission could not be resolved")
            return canonical_user_id, [activity_reply]

        try:
            get_business_profile(actor=actor)
        except ActivityNotFound:
            save_business_profile(
                actor=actor,
                activity_description=command.value,
                timezone_name=settings.TIMEZONE,
            )
        else:
            activity_reply = _owner_control_reply(
                canonical_user_id=canonical_user_id,
                platform=platform,
                accesses=accesses,
                raw_text=f"деятельность {command.value}",
                business_id=actor.business_id,
                interaction_key=interaction_key,
            )
            if activity_reply is None:
                raise RuntimeError("single-business activity update could not be resolved")
            return canonical_user_id, [activity_reply]
        reply = _owner_control_reply(
            canonical_user_id=canonical_user_id,
            platform=platform,
            accesses=accesses,
            raw_text="cpm:menu",
            business_id=actor.business_id,
            interaction_key=interaction_key,
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
            if len(accesses) > 1:
                _remember_business(
                    user_id=canonical_user_id,
                    platform=platform,
                    business_id=str(existing.business.id),
                )
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
        if accesses:
            _remember_business(
                user_id=canonical_user_id,
                platform=platform,
                business_id=str(access.business.id),
            )
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
            interaction_key=interaction_key,
        )
        if reply is not None:
            return canonical_user_id, [reply]
    if command.action == "start" and len(accesses) > 1:
        return canonical_user_id, [_business_selector_reply(accesses)]

    return canonical_user_id, [
        MessengerReply(text=_entry_text(platform=platform, accesses=accesses))
    ]


__all__ = [
    "ClientPlatformEntryCommand",
    "handle_clientplatform_entry",
    "parse_clientplatform_entry_command",
]
